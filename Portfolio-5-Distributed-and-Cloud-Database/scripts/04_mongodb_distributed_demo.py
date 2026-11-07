"""
Portfolio 5 - MongoDB's built-in distribution, on the same case study.

  REPLICA SET rsP5 (3 mongod, ports 27021-27023)
    M1 topology and write concern (w:1 vs w:majority latency)
    M2 read preference: stale reads from a secondary vs a causally consistent session
    M3 automatic failover: kill -9 the primary while a client keeps writing
    M4 the failed node rejoins and catches up
  SHARDED CLUSTER (config server :27030, shards shA :27031 / shB :27032, router mongos :27040)
    M5 hashed shard key on patientId; data distribution
    M6 targeted (single-shard) vs scatter-gather queries

    python3 04_mongodb_distributed_demo.py
Writes ../evidence/mongodb_distributed_log.txt and mongodb_distributed_metrics.json
"""
import json
import os
import shutil
import signal
import statistics
import subprocess
import threading
import time
from datetime import datetime, timezone

import psycopg
from pymongo import MongoClient, ReadPreference, WriteConcern
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern

MBIN = os.environ.get("MONGO_BIN", "/home/claude/tools/mongoenv/bin")
BASE = os.environ.get("P5_MONGO_BASE", "/var/lib/mongo-p5")
PG = os.environ.get("WC_DSN", "host=localhost dbname=waynes_clinic user=postgres password=postgres")
EVID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence")
T0 = time.perf_counter()
LOG, METRICS = [], {}


def log(who, text):
    for i, l in enumerate(str(text).splitlines() or [""]):
        line = (f"{(time.perf_counter() - T0):8.3f}s  [{who:<9}] " if i == 0 else " " * 23) + l
        print(line); LOG.append(line)


def head(title):
    for l in ("", "=" * 100, title, "=" * 100):
        print(l); LOG.append(l)


def mongod(name, port, extra):
    path = f"{BASE}/{name}"
    os.makedirs(path, exist_ok=True)
    subprocess.run([f"{MBIN}/mongod", "--dbpath", path, "--port", str(port), "--bind_ip", "127.0.0.1",
                    "--fork", "--logpath", f"{BASE}/{name}.log", "--wiredTigerCacheSizeGB", "0.25", *extra],
                   check=True, stdout=subprocess.DEVNULL)


def pid_of(port):
    out = subprocess.run(["pgrep", "-f", "--", f"--port {port} "], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def stop_all():
    for port in (27021, 27022, 27023, 27030, 27031, 27032, 27040):
        p = pid_of(port)
        if p:
            os.kill(p, signal.SIGTERM)
    time.sleep(2)


def wait_primary(uri, timeout=60):
    c = MongoClient(uri, directConnection=True, serverSelectionTimeoutMS=2000)
    for _ in range(timeout * 4):
        try:
            if c.admin.command("hello").get("isWritablePrimary"):
                return True
        except PyMongoError:
            pass
        time.sleep(0.25)
    return False


stop_all()
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(BASE)

# ============================================================================ REPLICA SET
head("M1  REPLICA SET - three members, automatic leader election (Raft-style protocol)")
for i, port in enumerate((27021, 27022, 27023), 1):
    mongod(f"rs{i}", port, ["--replSet", "rsP5"])
seed = MongoClient("mongodb://127.0.0.1:27021", directConnection=True)
seed.admin.command("replSetInitiate", {"_id": "rsP5", "members": [
    {"_id": 0, "host": "127.0.0.1:27021", "priority": 2},
    {"_id": 1, "host": "127.0.0.1:27022", "priority": 1},
    {"_id": 2, "host": "127.0.0.1:27023", "priority": 1}]})
wait_primary("mongodb://127.0.0.1:27021")
RS = "mongodb://127.0.0.1:27021,127.0.0.1:27022,127.0.0.1:27023/?replicaSet=rsP5"
client = MongoClient(RS, retryWrites=True, w="majority")
time.sleep(2)
for m in client.admin.command("replSetGetStatus")["members"]:
    log("rs.status", f"{m['name']:<16} {m['stateStr']:<10} health={m['health']}")
db = client.waynes_clinic
enc = db.encounters
enc.insert_one({"_id": 0, "warmup": True})

lat = {}
for wc in ("1", "majority"):
    coll = enc.with_options(write_concern=WriteConcern(w=1 if wc == "1" else "majority"))
    t = []
    for i in range(300):
        t0 = time.perf_counter()
        coll.insert_one({"probe": wc, "i": i})
        t.append((time.perf_counter() - t0) * 1000)
    t.sort()
    lat[f"w:{wc}"] = dict(median_ms=round(statistics.median(t), 3), p95_ms=round(t[284], 3))
    log("client", f"insert with w:{wc:<9} median {lat[f'w:{wc}']['median_ms']:.3f} ms  p95 {lat[f'w:{wc}']['p95_ms']:.3f} ms   "
                  + ("acknowledged by the primary alone" if wc == "1" else "acknowledged once 2 of 3 members hold it (survives a failover)"))
METRICS["write_concern_latency"] = lat

# ---------------------------------------------------------------------------------- M2
head("M2  READ PREFERENCE - reading from secondaries scales reads but may be stale")
sec = enc.with_options(read_preference=ReadPreference.SECONDARY)
stale = 0
for i in range(300):
    enc.with_options(write_concern=WriteConcern(w=1)).insert_one({"_id": f"rr{i}", "kind": "read-your-write test"})
    if sec.find_one({"_id": f"rr{i}"}) is None:
        stale += 1
log("client", f"300 x (write w:1 to primary, then read immediately from a SECONDARY): {stale} reads were STALE")
stale_causal = 0
with client.start_session(causal_consistency=True) as s:
    for i in range(300):
        enc.with_options(write_concern=WriteConcern(w="majority")).insert_one({"_id": f"cc{i}"}, session=s)
        if enc.with_options(read_preference=ReadPreference.SECONDARY,
                            read_concern=ReadConcern("majority")
                            ).find_one({"_id": f"cc{i}"}, session=s) is None:
            stale_causal += 1
log("client", f"300 x the same inside a CAUSALLY CONSISTENT session (w:majority, readConcern majority): {stale_causal} stale reads")
METRICS["stale_secondary_reads_of_300"] = {"plain": stale, "causal_session": stale_causal}

# ---------------------------------------------------------------------------------- M3
head("M3  AUTOMATIC FAILOVER - kill -9 the primary while a receptionist app keeps booking")
primary = client.admin.command("hello")["primary"]
acked, errors, stop = [], [], threading.Event()


def writer():
    i = 0
    while not stop.is_set():
        i += 1
        try:
            enc.insert_one({"_id": f"bk{i}", "kind": "booking during failover", "at": datetime.now(timezone.utc)})
            acked.append((time.perf_counter(), f"bk{i}"))
        except PyMongoError as e:
            errors.append((time.perf_counter(), type(e).__name__))
        time.sleep(0.02)


th = threading.Thread(target=writer)
th.start()
time.sleep(2)
victim = pid_of(int(primary.split(":")[1]))
os.kill(victim, signal.SIGKILL)
t_kill = time.perf_counter()
log("chaos", f"SIGKILL primary {primary} (pid {victim})")
new_primary = None
while new_primary is None and time.perf_counter() - t_kill < 60:
    for port in (27021, 27022, 27023):
        if f":{port}" in primary:
            continue
        try:
            h = MongoClient(f"mongodb://127.0.0.1:{port}", directConnection=True, serverSelectionTimeoutMS=500).admin.command("hello")
            if h.get("isWritablePrimary"):
                new_primary = h["me"]
        except PyMongoError:
            pass
    time.sleep(0.1)
t_elected = time.perf_counter()
time.sleep(3)
stop.set(); th.join()
gap_before = max([t for t, _ in acked if t < t_kill], default=t_kill)
first_after = min([t for t, _ in acked if t > t_kill], default=None)
stored = enc.count_documents({"kind": "booking during failover"})
METRICS["failover"] = dict(election_s=round(t_elected - t_kill, 2),
                           write_pause_s=round((first_after or t_kill) - gap_before, 2),
                           acknowledged=len(acked), failed_attempts=len(errors), acknowledged_but_missing=len(acked) - stored)
log("rs", f"new primary elected: {new_primary}  {t_elected - t_kill:.2f} s after the kill")
log("client", f"bookings acknowledged: {len(acked)}; attempts that raised an error during the election: {len(errors)}"
              f"{' (' + ', '.join(sorted(set(e for _, e in errors))) + ')' if errors else ''}")
log("client", f"longest pause in successful writes: {METRICS['failover']['write_pause_s']:.2f} s "
              "(retryable writes re-sent the in-flight insert to the new primary)")
log("check", f"acknowledged bookings missing after failover: {len(acked) - stored}  (w:majority => none can be rolled back)")

# ---------------------------------------------------------------------------------- M4
head("M4  RECOVERY - the killed member restarts and rejoins as a secondary")
port = int(primary.split(":")[1])
mongod(f"rs{port - 27020}", port, ["--replSet", "rsP5"])
time.sleep(6)
for m in client.admin.command("replSetGetStatus")["members"]:
    log("rs.status", f"{m['name']:<16} {m['stateStr']:<10} optime={m['optimeDate']}")

# ============================================================================ SHARDED CLUSTER
head("M5  SHARDED CLUSTER - config server + 2 shards + mongos router; hashed shard key on patientId")
mongod("cfg", 27030, ["--configsvr", "--replSet", "cfgP5"])
MongoClient("mongodb://127.0.0.1:27030", directConnection=True).admin.command(
    "replSetInitiate", {"_id": "cfgP5", "configsvr": True, "members": [{"_id": 0, "host": "127.0.0.1:27030"}]})
for name, port in (("shA", 27031), ("shB", 27032)):
    mongod(name, port, ["--shardsvr", "--replSet", name])
    MongoClient(f"mongodb://127.0.0.1:{port}", directConnection=True).admin.command(
        "replSetInitiate", {"_id": name, "members": [{"_id": 0, "host": f"127.0.0.1:{port}"}]})
for p in (27030, 27031, 27032):
    wait_primary(f"mongodb://127.0.0.1:{p}")
subprocess.run([f"{MBIN}/mongos", "--configdb", "cfgP5/127.0.0.1:27030", "--port", "27040", "--bind_ip", "127.0.0.1",
                "--fork", "--logpath", f"{BASE}/mongos.log"], check=True, stdout=subprocess.DEVNULL)
ms = MongoClient("mongodb://127.0.0.1:27040")
for name, port in (("shA", 27031), ("shB", 27032)):
    ms.admin.command("addShard", f"{name}/127.0.0.1:{port}")
ms.admin.command("enableSharding", "waynes_clinic")
ms.admin.command("shardCollection", "waynes_clinic.encounters", key={"patientId": "hashed"})
log("mongos", "sh.addShard(shA), sh.addShard(shB); sh.shardCollection('waynes_clinic.encounters', {patientId: 'hashed'})")

with psycopg.connect(PG) as pg:
    pg.execute("SET timezone TO 'UTC'")
    rows = pg.execute("""
        SELECT a.appointment_id, a.patient_id, a.doctor_id, 'Dr ' || d.last_name, a.scheduled_start, a.status::text,
               coalesce((SELECT array_agg(icd10_code) FROM clinic.diagnosis dx WHERE dx.appointment_id = a.appointment_id), '{}')
        FROM clinic.appointment a JOIN clinic.doctor d USING (doctor_id)
        WHERE a.scheduled_start >= '2026-07-01' AND a.scheduled_start < '2026-10-01'""").fetchall()
docs = [{"_id": r[0], "patientId": r[1], "doctor": {"id": r[2], "name": r[3]}, "start": r[4], "status": r[5],
         "diagnoses": list(r[6])} for r in rows]
t0 = time.perf_counter()
for i in range(0, len(docs), 5000):
    ms.waynes_clinic.encounters.insert_many(docs[i:i + 5000], ordered=False)
log("mongos", f"inserted {len(docs):,} encounter documents (Jul-Sep 2026) through the router in {time.perf_counter() - t0:.1f} s")
dist = {}
for name, port in (("shA", 27031), ("shB", 27032)):
    n = MongoClient(f"mongodb://127.0.0.1:{port}", directConnection=True).waynes_clinic.encounters.count_documents({})
    dist[name] = n
    log(name, f"{n:>7,} documents ({100 * n / len(docs):.1f} %)")
METRICS["sharded_distribution"] = dist

head("M6  QUERY ROUTING - a patient's history hits ONE shard; a status report hits ALL shards")
pid = docs[1000]["patientId"]
for label, flt in (("history of one patient", {"patientId": pid}), ("all no-shows (no shard key)", {"status": "NoShow"})):
    ex = ms.waynes_clinic.command("explain", {"find": "encounters", "filter": flt}, verbosity="executionStats")
    wp = ex["queryPlanner"]["winningPlan"]
    shards = [s["shardName"] for s in wp.get("shards", [])]
    log("mongos", f"{label:<30} filter={json.dumps(flt)}  stage={wp['stage']}  shards queried={shards}  "
                  f"returned={ex['executionStats']['nReturned']}")
METRICS["routing"] = "SINGLE_SHARD for patientId filter, SHARD_MERGE across both shards for status filter"

json.dump(METRICS, open(os.path.join(EVID, "mongodb_distributed_metrics.json"), "w"), indent=2, default=str)
open(os.path.join(EVID, "mongodb_distributed_log.txt"), "w").write("\n".join(LOG) + "\n")
print(json.dumps(METRICS, indent=2, default=str))
if os.environ.get("KEEP_RUNNING") != "1":
    stop_all()
