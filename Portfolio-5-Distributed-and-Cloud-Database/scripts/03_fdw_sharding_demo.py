"""
Portfolio 5 - horizontal sharding of appointments by patient with native
PostgreSQL features: a hash-partitioned table on a coordinator whose
partitions are foreign tables (postgres_fdw) living on two shard servers.

  S1 build the shards and route ~300k 2026 appointments through the coordinator
  S2 distribution of rows across shards
  S3 single-shard query: partition pruning sends the query to ONE shard
  S4 cross-shard aggregate: scatter-gather with aggregate push-down and async scans
  S5 routed insert
  S6 shard failure: partial availability
  S7 cross-shard transactions are NOT atomic without 2PC (discussion evidence)

Needs the main clinic database (Portfolio 1+2) on :5432 and the nodes from
01_setup_pg_nodes.sh on :5440-5442.   sudo python3 03_fdw_sharding_demo.py
"""
import json
import os
import statistics
import subprocess
import time

import psycopg

BIN = os.environ.get("PG_BIN", "/usr/lib/postgresql/16/bin")
BASE = os.environ.get("P5_BASE", "/var/lib/postgresql/p5")
MAIN = dict(host="localhost", port=5432, user="postgres", password=os.environ.get("PGPASSWORD", "postgres"))
EVID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence")
COORD, SH1, SH2 = 5440, 5441, 5442
T0 = time.perf_counter()
LOG, METRICS = [], {}


def log(node, text):
    for i, l in enumerate(str(text).splitlines() or [""]):
        line = (f"{(time.perf_counter() - T0):8.3f}s  [{node:<11}] " if i == 0 else " " * 25) + l
        print(line); LOG.append(line)


def head(title):
    for l in ("", "=" * 100, title, "=" * 100):
        print(l); LOG.append(l)


def c(port, db="postgres"):
    return psycopg.connect(host="localhost", port=port, dbname=db, user="postgres", autocommit=True, connect_timeout=3)


SHARD_DDL = """
CREATE TABLE appointment_shard (
    appointment_id  bigint PRIMARY KEY,
    patient_id      integer NOT NULL,
    doctor_id       integer NOT NULL,
    scheduled_start timestamptz NOT NULL,
    scheduled_end   timestamptz NOT NULL,
    status          text NOT NULL,
    reason          text);
CREATE INDEX ON appointment_shard (patient_id, scheduled_start);"""

# ------------------------------------------------------------------------------ S1
head("S1  BUILD - hash-partition appointment by patient_id across two shard servers")
for port in (SH1, SH2):
    with c(port) as k:
        k.execute("DROP DATABASE IF EXISTS clinic_shard")
        k.execute("CREATE DATABASE clinic_shard")
    with c(port, "clinic_shard") as k:
        for stmt in SHARD_DDL.split(";"):
            if stmt.strip():
                k.execute(stmt)
    log(f"shard:{port}", "database clinic_shard + table appointment_shard created")

with c(COORD) as k:
    k.execute("DROP DATABASE IF EXISTS clinic_coordinator")
    k.execute("CREATE DATABASE clinic_coordinator")
coord_ddl = f"""
CREATE EXTENSION postgres_fdw;
CREATE SERVER shard1 FOREIGN DATA WRAPPER postgres_fdw
       OPTIONS (host 'localhost', port '{SH1}', dbname 'clinic_shard', async_capable 'true', batch_size '1000', fetch_size '10000');
CREATE SERVER shard2 FOREIGN DATA WRAPPER postgres_fdw
       OPTIONS (host 'localhost', port '{SH2}', dbname 'clinic_shard', async_capable 'true', batch_size '1000', fetch_size '10000');
CREATE SERVER main_db FOREIGN DATA WRAPPER postgres_fdw
       OPTIONS (host 'localhost', port '5432', dbname 'waynes_clinic', fetch_size '10000');
CREATE USER MAPPING FOR postgres SERVER shard1  OPTIONS (user 'postgres');
CREATE USER MAPPING FOR postgres SERVER shard2  OPTIONS (user 'postgres');
CREATE USER MAPPING FOR postgres SERVER main_db OPTIONS (user 'postgres', password '{MAIN['password']}');

-- the logical, cluster-wide table: rows are routed by hash(patient_id)
CREATE TABLE appointment (
    appointment_id  bigint NOT NULL,
    patient_id      integer NOT NULL,
    doctor_id       integer NOT NULL,
    scheduled_start timestamptz NOT NULL,
    scheduled_end   timestamptz NOT NULL,
    status          text NOT NULL,
    reason          text
) PARTITION BY HASH (patient_id);
CREATE FOREIGN TABLE appointment_s1 PARTITION OF appointment FOR VALUES WITH (MODULUS 2, REMAINDER 0)
       SERVER shard1 OPTIONS (table_name 'appointment_shard');
CREATE FOREIGN TABLE appointment_s2 PARTITION OF appointment FOR VALUES WITH (MODULUS 2, REMAINDER 1)
       SERVER shard2 OPTIONS (table_name 'appointment_shard');

-- the source: the clinic's real appointment table on the main server
CREATE FOREIGN TABLE source_appointment (
    appointment_id bigint, patient_id integer, doctor_id integer, scheduled_start timestamptz,
    scheduled_end timestamptz, status text, reason text)
  SERVER main_db OPTIONS (schema_name 'clinic', table_name 'appointment');"""
with c(COORD, "clinic_coordinator") as k:
    for stmt in coord_ddl.split(";"):
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if body:
            k.execute(body)
    log("coordinator", "postgres_fdw servers shard1/shard2, partitioned table appointment (HASH patient_id, modulus 2)")
    t0 = time.perf_counter()
    n = k.execute("""INSERT INTO appointment
                     SELECT appointment_id, patient_id, doctor_id, scheduled_start, scheduled_end, status::text, reason
                     FROM source_appointment
                     WHERE scheduled_start >= '2026-01-01' AND scheduled_start < '2027-01-01'""").rowcount
    METRICS["rows_routed"] = n
    METRICS["load_seconds"] = round(time.perf_counter() - t0, 1)
    log("coordinator", f"INSERT ... SELECT routed {n:,} 2026 appointments to the shards in {METRICS['load_seconds']} s")
for port in (SH1, SH2):
    with c(port, "clinic_shard") as k:
        k.execute("ANALYZE appointment_shard")
with c(COORD, "clinic_coordinator") as k:
    k.execute("ALTER FOREIGN TABLE appointment_s1 OPTIONS (ADD use_remote_estimate 'true')")
    k.execute("ALTER FOREIGN TABLE appointment_s2 OPTIONS (ADD use_remote_estimate 'true')")

# ------------------------------------------------------------------------------ S2
head("S2  DISTRIBUTION - each shard holds about half of the rows; a patient lives on exactly one shard")
dist = {}
for name, port in (("shard1", SH1), ("shard2", SH2)):
    with c(port, "clinic_shard") as k:
        rows, pats = k.execute("SELECT count(*), count(DISTINCT patient_id) FROM appointment_shard").fetchone()
        size = k.execute("SELECT pg_size_pretty(pg_total_relation_size('appointment_shard'))").fetchone()[0]
    dist[name] = dict(rows=rows, patients=pats)
    log(name, f"{rows:>8,} appointments for {pats:>6,} patients  ({size})")
METRICS["distribution"] = dist
with c(SH1, "clinic_shard") as a, c(SH2, "clinic_shard") as b:
    s1 = {r[0] for r in a.execute("SELECT DISTINCT patient_id FROM appointment_shard")}
    s2 = {r[0] for r in b.execute("SELECT DISTINCT patient_id FROM appointment_shard")}
log("check", f"patients present on BOTH shards: {len(s1 & s2)}  (co-location: a patient's history is never split)")

# ------------------------------------------------------------------------------ S3
head("S3  SINGLE-SHARD QUERY - partition pruning sends a patient's history to one shard only")
with c(COORD, "clinic_coordinator") as k:
    pid = k.execute("SELECT patient_id FROM appointment GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1 OFFSET 500").fetchone()[0]
    q = f"SELECT appointment_id, scheduled_start, status FROM appointment WHERE patient_id = {pid} ORDER BY scheduled_start DESC"
    plan = "\n".join(r[0] for r in k.execute("EXPLAIN (VERBOSE, COSTS OFF) " + q))
    log("coordinator", q)
    log("coordinator", plan)
    t = []
    for _ in range(50):
        t0 = time.perf_counter(); k.execute(q).fetchall(); t.append((time.perf_counter() - t0) * 1000)
    METRICS["single_shard_query_ms"] = round(statistics.median(t), 2)
    log("coordinator", f"median latency over 50 runs: {METRICS['single_shard_query_ms']} ms")

# ------------------------------------------------------------------------------ S4
head("S4  CROSS-SHARD QUERIES - scatter-gather, and when aggregation can be pushed to the shards")
with c(COORD, "clinic_coordinator") as k:
    k.execute("SET enable_partitionwise_aggregate = on")
    q = "SELECT status, count(*) FROM appointment GROUP BY status ORDER BY status"
    log("coordinator", "(a) GROUP BY a column that is NOT the shard key:")
    log("coordinator", q)
    log("coordinator", "\n".join(r[0] for r in k.execute("EXPLAIN (VERBOSE, COSTS OFF) " + q)))
    log("coordinator", "result: " + ", ".join(f"{st}={n:,}" for st, n in k.execute(q).fetchall()))
    t = []
    for _ in range(10):
        t0 = time.perf_counter(); k.execute(q).fetchall(); t.append((time.perf_counter() - t0) * 1000)
    METRICS["cross_shard_status_count_ms"] = round(statistics.median(t), 1)
    log("coordinator", f"median {METRICS['cross_shard_status_count_ms']} ms. Remote SQL is a bare 'SELECT status': every one of the "
                       f"{METRICS['rows_routed']:,} values crosses the network and is partially aggregated on the coordinator "
                       "(postgres_fdw in PG16 cannot push a PARTIAL aggregate to a shard).")
    q2 = ("SELECT patient_id, count(*) AS visits FROM appointment GROUP BY patient_id "
          "HAVING count(*) >= 20 ORDER BY visits DESC, patient_id LIMIT 5")
    log("coordinator", "(b) GROUP BY the shard key: each group lives on one shard, so the whole aggregate is pushed down:")
    log("coordinator", q2)
    log("coordinator", "\n".join(r[0] for r in k.execute("EXPLAIN (VERBOSE, COSTS OFF) " + q2)))
    log("coordinator", "result: " + ", ".join(f"patient {pid}: {n}" for pid, n in k.execute(q2).fetchall()))
    t = []
    for _ in range(10):
        t0 = time.perf_counter(); k.execute(q2).fetchall(); t.append((time.perf_counter() - t0) * 1000)
    METRICS["cross_shard_pushed_down_aggregate_ms"] = round(statistics.median(t), 1)
    log("coordinator", f"median {METRICS['cross_shard_pushed_down_aggregate_ms']} ms. Remote SQL contains GROUP BY ... HAVING: the shards "
                       "aggregate in parallel (Async Foreign Scan) and only the qualifying groups are returned. "
                       "Lesson: choose the shard key to match the heaviest queries.")

# ------------------------------------------------------------------------------ S5
head("S5  ROUTED WRITE - the coordinator decides which shard stores a new booking")
with c(COORD, "clinic_coordinator") as k:
    picks = {}
    cand = 900001
    while len(picks) < 2:                       # one new patient for each shard
        rem = 0 if k.execute("SELECT satisfies_hash_partition('appointment'::regclass, 2, 0, %s::int)", (cand,)).fetchone()[0] else 1
        picks.setdefault(rem, cand)
        cand += 1
    new_patients = [picks[0], picks[1]]
    for new_pid in new_patients:
        k.execute("""INSERT INTO appointment VALUES (%s, %s, 1, '2026-12-01 14:00+00', '2026-12-01 14:30+00', 'Scheduled', 'Sharding demo')""",
                  (9_000_000 + new_pid, new_pid))
        where = k.execute("SELECT tableoid::regclass::text FROM appointment WHERE patient_id = %s", (new_pid,)).fetchone()[0]
        log("coordinator", f"INSERT appointment for patient {new_pid} -> routed to partition {where}")

# ------------------------------------------------------------------------------ S6
head("S6  SHARD FAILURE - shard2 goes down")
subprocess.run(["su", "postgres", "-c", f"{BIN}/pg_ctl -D {BASE}/shard2 -w stop -m fast"], check=True, stdout=subprocess.DEVNULL)
log("shard2", "server stopped")
with c(COORD, "clinic_coordinator") as k:
    for test_pid in new_patients:
        try:
            n = k.execute("SELECT count(*) FROM appointment WHERE patient_id = %s", (test_pid,)).fetchone()[0]
            log("coordinator", f"history of patient {test_pid}: OK ({n} row) - their shard is up")
        except psycopg.Error as e:
            log("coordinator", f"history of patient {test_pid}: ERROR {e.sqlstate} {e.diag.message_primary.splitlines()[0]}")
    try:
        k.execute("SELECT count(*) FROM appointment").fetchone()
    except psycopg.Error as e:
        log("coordinator", f"clinic-wide count: ERROR {e.sqlstate} {e.diag.message_primary.splitlines()[0]}")
log("analysis", "Partial availability: patients on the healthy shard are served; anything touching shard2 fails.\n"
                "In production each shard is itself a primary + standby pair (D-series) so a node loss triggers failover, not an outage.")
subprocess.run(["su", "postgres", "-c", f"{BIN}/pg_ctl -D {BASE}/shard2 -l {BASE}/logs/shard2.log -w start"], check=True, stdout=subprocess.DEVNULL)
log("shard2", "server restarted")

# ------------------------------------------------------------------------------ S7
head("S7  CROSS-SHARD TRANSACTIONS - postgres_fdw commits each shard separately (no two-phase commit)")
with c(COORD, "clinic_coordinator") as k:
    k.execute("BEGIN")
    k.execute("UPDATE appointment SET status = 'Cancelled' WHERE patient_id = ANY(%s)", (new_patients,))
    k.execute("COMMIT")
    log("coordinator", "UPDATE touching both shards committed: postgres_fdw opened a remote transaction on each shard and committed them\n"
                       "one after the other. If a shard failed between the two remote COMMITs, one change would persist and the other\n"
                       "would not. Citus and similar systems close this gap with two-phase commit (PREPARE TRANSACTION / COMMIT PREPARED).")
    k.execute("DELETE FROM appointment WHERE patient_id = ANY(%s)", (new_patients,))

open(os.path.join(EVID, "sharding_fdw_log.txt"), "w").write("\n".join(LOG) + "\n")
json.dump(METRICS, open(os.path.join(EVID, "sharding_metrics.json"), "w"), indent=2)
print(json.dumps(METRICS, indent=2))
