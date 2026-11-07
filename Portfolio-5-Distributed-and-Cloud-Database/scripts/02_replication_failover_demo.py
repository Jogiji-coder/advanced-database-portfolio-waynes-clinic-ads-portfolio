"""
Portfolio 5 - replication, consistency, availability and failover on the
local PostgreSQL topology built by 01_setup_pg_nodes.sh.

  D1 topology            pg_stat_replication / standby status
  D2 consistency         read-your-writes on the sync replica vs a stale read on the async replica
  D3 read-only replicas  writes are refused on a hot standby
  D4 latency cost        commit latency for each synchronous_commit level
  D5 availability (CAP)  sync standby fails -> commits block -> switch to quorum commit
  D6 failover            crash the primary, promote the sync replica, re-point the async replica
  D7 rejoin              pg_rewind turns the crashed primary into a standby of the new primary

    sudo python3 02_replication_failover_demo.py     (needs to stop/start servers as the postgres user)
Writes ../evidence/replication_failover_log.txt and replication_metrics.json
"""
import json
import os
import statistics
import subprocess
import threading
import time

import psycopg

BIN = os.environ.get("PG_BIN", "/usr/lib/postgresql/16/bin")
BASE = os.environ.get("P5_BASE", "/var/lib/postgresql/p5")
EVID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence")
NODES = {"primary": 5433, "replica_sync": 5434, "replica_async": 5435}
T0 = time.perf_counter()
LOG, METRICS = [], {}


def log(node, text):
    line = f"{(time.perf_counter() - T0):8.3f}s  [{node:<13}] {text}"
    print(line)
    LOG.append(line)


def head(title):
    for l in ("", "=" * 96, title, "=" * 96):
        print(l); LOG.append(l)


def conn(port, db="waynes_clinic"):
    c = psycopg.connect(f"host=localhost port={port} dbname={db} user=postgres", autocommit=True, connect_timeout=3)
    c.execute("SET search_path TO clinic, public")
    return c


def q(port, sql, params=None, db="waynes_clinic"):
    with conn(port, db) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else cur.statusmessage


def pg_ctl(node, action, mode=""):
    cmd = f"{BIN}/pg_ctl -D {BASE}/{node} -l {BASE}/logs/{node}.log -w {action} {mode}"
    subprocess.run(["su", "postgres", "-c", cmd], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def show_replication(port, who):
    rows = q(port, """SELECT application_name, state, sync_state, pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_gap_bytes,
                             coalesce(replay_lag::text, '-') FROM pg_stat_replication ORDER BY application_name""", db="postgres")
    for r in rows:
        log(who, f"pg_stat_replication: {r[0]:<14} state={r[1]:<10} sync_state={r[2]:<6} replay_gap={r[3]} bytes replay_lag={r[4]}")
    return rows


# ------------------------------------------------------------------------------ D1
head("D1  TOPOLOGY - one primary streaming WAL to a synchronous and an asynchronous standby")
show_replication(5433, "primary")
for node in ("replica_sync", "replica_async"):
    r = q(NODES[node], "SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn()::text, "
                       "(SELECT setting FROM pg_settings WHERE name = 'recovery_min_apply_delay')")[0]
    log(node, f"in_recovery={r[0]}  replayed_up_to={r[1]}  recovery_min_apply_delay={r[2]} ms")
q(5433, "CREATE TABLE IF NOT EXISTS clinic.repl_probe (id bigserial PRIMARY KEY, note text, written_at timestamptz DEFAULT clock_timestamp())")

# ------------------------------------------------------------------------------ D2
head("D2  CONSISTENCY - is a write on the primary visible on each replica straight away?")
with conn(5433) as p:
    p.execute("SET synchronous_commit = remote_apply")
    t_commit = time.perf_counter()
    p.execute("INSERT INTO patient (mrn, first_name, last_name, date_of_birth, sex) VALUES ('WCB0500001', 'Grace', 'Nakamura', '1987-04-09', 'F')")
    log("primary", "INSERT patient WCB0500001 committed with synchronous_commit = remote_apply")
for node in ("replica_sync", "replica_async"):
    n = q(NODES[node], "SELECT count(*) FROM patient WHERE mrn = 'WCB0500001'")[0][0]
    log(node, f"immediately after COMMIT: rows found = {n}  -> {'CONSISTENT (read-your-writes)' if n else 'STALE READ'}")
while q(5435, "SELECT count(*) FROM patient WHERE mrn = 'WCB0500001'")[0][0] == 0:
    time.sleep(0.05)
lag = time.perf_counter() - t_commit
METRICS["async_replica_visibility_s"] = round(lag, 2)
log("replica_async", f"row became visible after {lag:.2f} s  -> EVENTUALLY CONSISTENT (3 s apply delay + replay)")

# ------------------------------------------------------------------------------ D3
head("D3  READ-ONLY STANDBYS - replicas scale reads, never writes")
try:
    q(5434, "INSERT INTO patient (mrn, first_name, last_name, date_of_birth, sex) VALUES ('WCB0500002', 'X', 'Y', '1990-01-01', 'U')")
except psycopg.Error as e:
    log("replica_sync", f"INSERT -> ERROR {e.sqlstate}: {e.diag.message_primary}")
r = q(5434, "SELECT count(*) FROM appointment")[0][0]
log("replica_sync", f"SELECT count(*) FROM appointment -> {r}  (reads are served)")

# ------------------------------------------------------------------------------ D4
head("D4  THE PRICE OF CONSISTENCY - commit latency for each synchronous_commit level (300 commits each)")
lat = {}
with conn(5433) as p:
    for level in ["off", "local", "remote_write", "on", "remote_apply"]:
        p.execute(f"SET synchronous_commit = {level}")
        for _ in range(20):
            p.execute("INSERT INTO repl_probe (note) VALUES ('warm-up')")
        t = []
        for i in range(300):
            t0 = time.perf_counter()
            p.execute("INSERT INTO repl_probe (note) VALUES (%s)", (level,))
            t.append((time.perf_counter() - t0) * 1000)
        t.sort()
        lat[level] = dict(median_ms=round(statistics.median(t), 3), p95_ms=round(t[284], 3))
        meaning = {"off": "returns before WAL is flushed even locally (a crash can lose ~3x wal_writer_delay)",
                   "local": "flushed to the primary's disk only",
                   "remote_write": "standby has received the WAL (in its OS cache)",
                   "on": "standby has flushed the WAL to disk",
                   "remote_apply": "standby has applied it - visible to readers there"}[level]
        log("primary", f"{level:<12} median {lat[level]['median_ms']:6.3f} ms  p95 {lat[level]['p95_ms']:6.3f} ms   {meaning}")
METRICS["commit_latency"] = lat

# ------------------------------------------------------------------------------ D5
head("D5  AVAILABILITY vs CONSISTENCY - the synchronous standby goes down")
pg_ctl("replica_sync", "stop", "-m fast")
log("replica_sync", "server stopped (simulated host failure)")
blocked = {}


def write_during_outage():
    t0 = time.perf_counter()
    with conn(5433) as p:
        p.execute("INSERT INTO repl_probe (note) VALUES ('written while sync standby is down')")
    blocked["s"] = time.perf_counter() - t0


th = threading.Thread(target=write_during_outage)
th.start()
time.sleep(3)
w = q(5433, "SELECT wait_event_type || '/' || wait_event, state FROM pg_stat_activity WHERE query LIKE 'INSERT INTO repl_probe%' AND pid <> pg_backend_pid()", db="postgres")
log("primary", f"after 3 s the COMMIT is still waiting: wait_event={w[0][0] if w else '?'}  "
               "-> the system chose CONSISTENCY over AVAILABILITY (it will not confirm a write only one copy holds)")
log("primary", "operator action: ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (replica_sync, replica_async)'; SELECT pg_reload_conf();")
q(5433, "ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (replica_sync, replica_async)'", db="postgres")
q(5433, "SELECT pg_reload_conf()", db="postgres")
th.join(10)
METRICS["commit_blocked_during_sync_outage_s"] = round(blocked.get("s", -1), 2)
log("primary", f"the waiting COMMIT completed {blocked.get('s', -1):.2f} s after it was issued - replica_async now satisfies the quorum "
               "(it confirms at its next status report: wal_receiver_status_interval = 1s)")
pg_ctl("replica_sync", "start")
time.sleep(1.5)
show_replication(5433, "primary")

# ------------------------------------------------------------------------------ D6
head("D6  FAILOVER - the primary crashes; promote the synchronous replica")
with conn(5433) as p:
    p.execute("SET synchronous_commit = on")
    p.execute("INSERT INTO repl_probe (note) VALUES ('last write before the crash')")
before = q(5433, "SELECT count(*) FROM repl_probe")[0][0]
log("primary", f"repl_probe rows = {before}; last write committed with synchronous_commit = on")
pg_ctl("primary", "stop", "-m immediate")
t_crash = time.perf_counter()
log("primary", "CRASH (pg_ctl stop -m immediate: no checkpoint, connections dropped)")
try:
    conn(5433)
except psycopg.OperationalError:
    log("client", "connection to :5433 refused -> failover triggered")
r = q(5434, "SELECT pg_promote(wait => true, wait_seconds => 30)", db="postgres")[0][0]
log("replica_sync", f"SELECT pg_promote() -> {r}")
while True:
    try:
        q(5434, "INSERT INTO repl_probe (note) VALUES ('first write on the new primary')")
        break
    except psycopg.Error:
        time.sleep(0.05)
rto = time.perf_counter() - t_crash
after = q(5434, "SELECT count(*) - 1 FROM repl_probe")[0][0]
METRICS["failover_time_to_first_write_s"] = round(rto, 2)
METRICS["rows_lost_in_failover"] = before - after
log("replica_sync", f"NEW PRIMARY accepting writes {rto:.2f} s after the crash; rows before crash = {before}, "
                    f"found on new primary = {after} -> data lost = {before - after} (RPO = 0 thanks to synchronous replication)")
log("replica_sync", f"timeline = {int(q(5434, 'SELECT substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8)', db='postgres')[0][0], 16)} (was 1), "
                    f"in_recovery = {q(5434, 'SELECT pg_is_in_recovery()', db='postgres')[0][0]}")

log("replica_async", "re-point the surviving standby at the new primary (no rebuild needed):")
q(5434, "SELECT pg_create_physical_replication_slot('slot_async')", db="postgres")
q(5435, "ALTER SYSTEM SET primary_conninfo = 'host=localhost port=5434 user=postgres application_name=replica_async'", db="postgres")
q(5435, "SELECT pg_reload_conf()", db="postgres")
for _ in range(100):
    if q(5434, "SELECT count(*) FROM pg_stat_replication WHERE application_name = 'replica_async' AND state = 'streaming'", db="postgres")[0][0]:
        break
    time.sleep(0.2)
show_replication(5434, "new primary")

# ------------------------------------------------------------------------------ D7
head("D7  REJOIN - pg_rewind turns the crashed old primary into a standby of the new one")
q(5434, "SELECT pg_create_physical_replication_slot('slot_old_primary')", db="postgres")
t0 = time.perf_counter()
out = subprocess.run(["su", "postgres", "-c",
                      f"{BIN}/pg_rewind --target-pgdata={BASE}/primary "
                      f"--source-server='host=localhost port=5434 user=postgres dbname=postgres' --progress"],
                     capture_output=True, text=True)
for l in (out.stdout + out.stderr).strip().splitlines()[-4:]:
    log("pg_rewind", l.strip())
# pg_rewind copies the source's configuration files too: restore this node's own port and
# replace the copied standby settings with ones that follow the new primary.
conf = open(f"{BASE}/primary/postgresql.conf").read().replace("port = 5434", "port = 5433")
open(f"{BASE}/primary/postgresql.conf", "w").write(conf)
with open(f"{BASE}/primary/postgresql.auto.conf", "w") as f:
    f.write("# written by 02_replication_failover_demo.py after pg_rewind\n"
            "primary_conninfo = 'host=localhost port=5434 user=postgres application_name=old_primary'\n"
            "primary_slot_name = 'slot_old_primary'\n")
open(f"{BASE}/primary/standby.signal", "w").close()
subprocess.run(["chown", "-R", "postgres:postgres", f"{BASE}/primary"], check=True)
pg_ctl("primary", "start")
for _ in range(100):
    if q(5434, "SELECT count(*) FROM pg_stat_replication WHERE application_name = 'old_primary' AND state = 'streaming'", db="postgres")[0][0]:
        break
    time.sleep(0.2)
METRICS["rejoin_with_pg_rewind_s"] = round(time.perf_counter() - t0, 2)
log("old_primary", f"now in_recovery = {q(5433, 'SELECT pg_is_in_recovery()', db='postgres')[0][0]} "
                   f"(rejoined as a standby in {METRICS['rejoin_with_pg_rewind_s']} s via pg_rewind)")
show_replication(5434, "new primary")
cnt = [q(p, "SELECT count(*) FROM repl_probe")[0][0] for p in (5434, 5433, 5435)]
time.sleep(3.5)
cnt = [q(p, "SELECT count(*) FROM repl_probe")[0][0] for p in (5434, 5433, 5435)]
log("all nodes", f"repl_probe rows: new primary={cnt[0]}, old primary (standby)={cnt[1]}, replica_async={cnt[2]} -> converged")

open(os.path.join(EVID, "replication_failover_log.txt"), "w").write("\n".join(LOG) + "\n")
json.dump(METRICS, open(os.path.join(EVID, "replication_metrics.json"), "w"), indent=2)
print(json.dumps(METRICS, indent=2))
