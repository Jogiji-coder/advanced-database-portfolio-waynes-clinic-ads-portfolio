"""
Patient-history latency: PostgreSQL (normalised, 4-branch UNION ALL of joins)
vs MongoDB (one indexed find on embedded encounter documents).

Same 300 randomly chosen patients, same machine, warm caches, each lookup
fully materialised on the client. Both databases have their recommended
indexes (Portfolio 2 for PostgreSQL, ix_patient_start for MongoDB).
    python 04_compare_history_latency.py
"""
import json
import os
import random
import statistics
import time

import psycopg
from pymongo import MongoClient

PG = os.environ.get("WC_DSN", "host=localhost dbname=waynes_clinic user=postgres password=postgres")
MONGO = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27017/?replicaSet=rs0")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence")

pg = psycopg.connect(PG)
pg.execute("SET search_path TO clinic, public")
mdb = MongoClient(MONGO).waynes_clinic
random.seed(42)
ids = [r[0] for r in pg.execute("SELECT patient_id FROM patient ORDER BY patient_id").fetchall()]
sample = random.sample(ids, 300)

SQL = "SELECT * FROM v_patient_history WHERE patient_id = %s ORDER BY event_time DESC"


def run_pg(pid):
    return len(pg.execute(SQL, (pid,)).fetchall())


def run_mongo(pid):
    return sum(1 for _ in mdb.encounters.find({"patientId": pid}).sort("start", -1))


def bench(fn):
    for pid in sample[:30]:          # warm-up
        fn(pid)
    t, n = [], []
    for pid in sample:
        t0 = time.perf_counter()
        n.append(fn(pid))
        t.append((time.perf_counter() - t0) * 1000)
    t.sort()
    return dict(mean_ms=round(statistics.mean(t), 3), median_ms=round(statistics.median(t), 3),
                p95_ms=round(t[int(0.95 * len(t)) - 1], 3), mean_items=round(statistics.mean(n), 1))


res = {"patients_sampled": len(sample),
       "postgresql_view_rows": bench(run_pg),
       "mongodb_encounter_documents": bench(run_mongo)}
pg_size = pg.execute("""SELECT pg_total_relation_size('appointment') + pg_total_relation_size('diagnosis')
    + pg_total_relation_size('prescription') + pg_total_relation_size('prescription_item')
    + pg_total_relation_size('lab_order') + pg_total_relation_size('lab_result')""").fetchone()[0]
st = mdb.command("collStats", "encounters")
res["storage"] = {"postgresql_clinical_tables_incl_indexes_MB": round(pg_size / 1e6, 1),
                  "mongodb_encounters_storage_plus_indexes_MB": round((st["storageSize"] + st["totalIndexSize"]) / 1e6, 1),
                  "mongodb_encounters_uncompressed_MB": round(st["size"] / 1e6, 1)}
print(json.dumps(res, indent=2))
json.dump(res, open(os.path.join(OUT, "history_latency_pg_vs_mongo.json"), "w"), indent=2)
