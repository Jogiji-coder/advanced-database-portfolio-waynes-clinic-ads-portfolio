"""
Portfolio 4 - key-value model (Redis 7) for Wayne's Clinic Baltimore.

Redis is NOT the system of record. It sits beside PostgreSQL for the jobs a
key-value store does best: short-lived locks, caching, queues and event
streams. Every command is printed in redis-cli style with its reply, so the
transcript in ../evidence/redis_demo_output.txt is the implementation evidence.

    python redis_demo.py
"""
import os
import statistics
import time

import psycopg
import redis

R = redis.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"), port=6379, decode_responses=True)
PG = psycopg.connect(os.environ.get("WC_DSN", "host=localhost dbname=waynes_clinic user=postgres password=postgres"))
PG.execute("SET search_path TO clinic, public")
OUT = []


def say(text=""):
    print(text)
    OUT.append(text)


def fmt(reply, indent=""):
    if reply is None:
        return "(nil)"
    if isinstance(reply, bool):
        return "(integer) " + str(int(reply))
    if isinstance(reply, int):
        return f"(integer) {reply}"
    if isinstance(reply, (list, tuple)):
        if not reply:
            return "(empty array)"
        return "\n".join(f"{indent}{i}) " + fmt(r, indent + "   ").lstrip() for i, r in enumerate(reply, 1))
    if isinstance(reply, dict):
        return fmt([x for kv in reply.items() for x in kv], indent)
    if reply == "OK":
        return "OK"
    return f'"{reply}"'


def cmd(*args):
    say("127.0.0.1:6379> " + " ".join(f'"{a}"' if " " in str(a) else str(a) for a in args))
    try:
        reply = R.execute_command(*args)
    except redis.ResponseError as e:
        reply = f"(error) {e}"
        say(reply)
        return None
    say("OK" if (str(args[0]).upper() == "SET" and reply is True) else fmt(reply))
    return reply


def section(title):
    say("")
    say("=" * 78)
    say(title)
    say("=" * 78)


R.flushdb()

# --------------------------------------------------------------------------------- R1
section("R1  SLOT HOLD - a 5-minute lock while a receptionist completes a booking (SET NX EX)")
slot = "slot:hold:doctor:1:2026-11-04T09:00"
say("# Receptionist A opens the booking form for Dr Okafor's 09:00 slot:")
cmd("SET", slot, "receptionist-A", "NX", "EX", 300)
say("# Receptionist B opens the same slot a second later - NX refuses because the key exists:")
cmd("SET", slot, "receptionist-B", "NX", "EX", 300)
cmd("GET", slot)
cmd("TTL", slot)
say("# B is told 'slot being booked by another user'. A completes the booking in PostgreSQL")
say("# (the EXCLUDE constraint is still the final guard), then releases the hold - but only")
say("# if A still owns it. A Lua script makes check-and-delete atomic:")
RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
cmd("EVAL", RELEASE, 1, slot, "receptionist-B")
cmd("EVAL", RELEASE, 1, slot, "receptionist-A")
cmd("EXISTS", slot)
say("# If A's browser crashes, the hold simply expires after 300 s - no stale lock remains.")

# --------------------------------------------------------------------------------- R2
section("R2  CACHE-ASIDE - patient summary card read on every screen")
SUMMARY_SQL = """
SELECT p.mrn, p.first_name || ' ' || p.last_name, p.date_of_birth::text, coalesce(p.blood_group, ''),
       coalesce((SELECT string_agg(m.generic_name || ' (' || pa.severity || ')', ', ')
                 FROM patient_allergy pa JOIN medication m USING (medication_id) WHERE pa.patient_id = p.patient_id), 'None'),
       coalesce((SELECT string_agg(DISTINCT dx.icd10_code, ', ') FROM diagnosis dx JOIN appointment a USING (appointment_id)
                 WHERE a.patient_id = p.patient_id AND dx.diagnosis_type <> 'Differential'), ''),
       (SELECT max(scheduled_start)::date::text FROM appointment a WHERE a.patient_id = p.patient_id AND a.status = 'Completed')
FROM patient p WHERE p.mrn = %s"""


def summary_from_pg(mrn):
    row = PG.execute(SUMMARY_SQL, (mrn,)).fetchone()
    return dict(zip(["mrn", "name", "dob", "blood_group", "allergies", "problems", "last_visit"], row))


key = "patient:WCB0000001:summary"
cmd("GET", key)
say("# MISS -> read from PostgreSQL, then store as a hash with a 10-minute TTL:")
t0 = time.perf_counter()
s = summary_from_pg("WCB0000001")
pg_ms = (time.perf_counter() - t0) * 1000
cmd("HSET", key, *[x for kv in s.items() for x in kv])
cmd("EXPIRE", key, 600)
cmd("HGETALL", key)
say("# When a doctor records a new allergy or diagnosis, the application deletes the key")
say("# (cache invalidation), so the next read repopulates it from the system of record.")

# measured latency, cache vs database
mrns = [r[0] for r in PG.execute("SELECT mrn FROM patient ORDER BY random() LIMIT 200").fetchall()]
for m in mrns:
    R.hset(f"patient:{m}:summary", mapping={k: (v or "") for k, v in summary_from_pg(m).items()})
pg_t, r_t = [], []
for m in mrns:
    t0 = time.perf_counter(); summary_from_pg(m); pg_t.append((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter(); R.hgetall(f"patient:{m}:summary"); r_t.append((time.perf_counter() - t0) * 1000)
say(f"# Measured over 200 patients: PostgreSQL median {statistics.median(pg_t):.3f} ms, "
    f"Redis HGETALL median {statistics.median(r_t):.3f} ms "
    f"({statistics.median(pg_t) / statistics.median(r_t):.0f}x faster from cache)")

# --------------------------------------------------------------------------------- R3
section("R3  EMERGENCY DEPARTMENT TRIAGE QUEUE (sorted set: priority, then arrival time)")
say("# score = triage_level * 10^10 + arrival epoch seconds  -> level 1 (resuscitation) always first")
arrivals = [("WCB0000011", 3, "19:02"), ("WCB0000019", 4, "19:05"), ("WCB0000003", 2, "19:07"),
            ("WCB0000020", 3, "19:09"), ("WCB0000007", 1, "19:12")]
for mrn, lvl, hhmm in arrivals:
    epoch = int(time.mktime(time.strptime("2026-10-28 " + hhmm, "%Y-%m-%d %H:%M")))
    cmd("ZADD", "ed:queue", lvl * 10**10 + epoch, f"{mrn}|triage{lvl}|{hhmm}")
cmd("ZCARD", "ed:queue")
cmd("ZRANGE", "ed:queue", 0, -1)
say("# Next patient for the free cubicle (atomic pop - two nurses can never take the same patient):")
cmd("ZPOPMIN", "ed:queue")
cmd("ZPOPMIN", "ed:queue")
cmd("ZRANGE", "ed:queue", 0, -1)

# --------------------------------------------------------------------------------- R4
section("R4  CRITICAL LAB ALERTS - Redis Streams with a consumer group")
crit = PG.execute("""SELECT p.mrn, r.test_code, r.result_value::text, to_char(r.resulted_at, 'YYYY-MM-DD HH24:MI')
                     FROM lab_result r JOIN lab_order lo USING (lab_order_id) JOIN appointment a USING (appointment_id)
                     JOIN patient p USING (patient_id)
                     WHERE r.abnormal_flag = 'C' ORDER BY r.resulted_at DESC LIMIT 3""").fetchall()
cmd("XGROUP", "CREATE", "lab:critical", "on-call-clinicians", "$", "MKSTREAM")
say("# The laboratory system appends one entry per critical result:")
for mrn, test, val, at in crit:
    cmd("XADD", "lab:critical", "*", "mrn", mrn, "test", test, "value", val, "resulted_at", at)
cmd("XLEN", "lab:critical")
say("# Dr Fairbanks' pager app reads new alerts for its consumer:")
read = R.execute_command("XREADGROUP", "GROUP", "on-call-clinicians", "dr-fairbanks", "COUNT", 2, "STREAMS", "lab:critical", ">")
say("127.0.0.1:6379> XREADGROUP GROUP on-call-clinicians dr-fairbanks COUNT 2 STREAMS lab:critical >")
ids = []
for stream, entries in read:
    for eid, fields in entries:
        ids.append(eid)
        say(f"   {eid}  " + "  ".join(f"{k}={v}" for k, v in fields.items()))
say("# One alert is acknowledged after the call-back; the other stays pending until handled:")
cmd("XACK", "lab:critical", "on-call-clinicians", ids[0])
cmd("XPENDING", "lab:critical", "on-call-clinicians")
say("# Unacknowledged alerts survive a consumer crash and can be re-assigned with XCLAIM.")

# --------------------------------------------------------------------------------- R5
section("R5  REAL-TIME DASHBOARD COUNTERS (atomic INCR / HINCRBY)")
cmd("HINCRBY", "stats:2026-10-28:checkins", "General Medicine", 1)
cmd("HINCRBY", "stats:2026-10-28:checkins", "General Medicine", 1)
cmd("HINCRBY", "stats:2026-10-28:checkins", "Emergency Medicine", 1)
cmd("INCR", "stats:2026-10-28:bookings")
cmd("EXPIRE", "stats:2026-10-28:checkins", 172800)
cmd("HGETALL", "stats:2026-10-28:checkins")

# --------------------------------------------------------------------------------- R6
section("R6  DURABILITY SETTINGS - why Redis is a cache here, not the record")
cmd("CONFIG", "GET", "appendonly")
cmd("CONFIG", "GET", "save")
say("# With appendonly=no, writes since the last RDB snapshot are lost on a crash. Acceptable for")
say("# holds, caches, counters and alerts (all re-creatable from PostgreSQL); NOT acceptable for")
say("# diagnoses or prescriptions. Production setting for the stream: appendonly yes + appendfsync everysec.")
cmd("INFO", "keyspace")

open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence", "redis_demo_output.txt"), "w").write("\n".join(OUT) + "\n")
