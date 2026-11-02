"""
Portfolio 2 - query optimisation benchmark harness (Wayne's Clinic Baltimore)

For each experiment it puts the database in the "before" state, runs the query
under EXPLAIN (ANALYZE, BUFFERS) seven times after a warm-up, records the median
execution time and buffer counts, saves the full text plan, applies the
optimisation and repeats. Writes:
    plans/E*_*.txt                    full execution plans
    results/benchmark_results.json    raw measurements
    results/benchmark_summary.md      summary table
Run after sql/01_bulk_load.sql:   python scripts/benchmark.py
"""
import json
import os
import re
import statistics
import time
from datetime import datetime

import psycopg

DSN = os.environ.get("WC_DSN", "host=localhost dbname=waynes_clinic user=postgres password=postgres")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANS = os.path.join(ROOT, "plans")
RESULTS = os.path.join(ROOT, "results")
RUNS = 7
SESSION = ["SET search_path TO clinic, public", "SET timezone TO 'America/New_York'",
           "SET jit = off"]          # JIT compile time adds noise to sub-second queries

DROP_ALL = """
DROP INDEX IF EXISTS ix_appt_patient_start, ix_prescription_appt, ix_lab_order_appt,
    ix_patient_last_first, ix_patient_last_pattern, ix_patient_last_trgm, ix_appt_doctor_start, ix_appt_doctor_start_cov,
    ix_lab_result_resulted_brin, ix_lab_result_critical;
DROP MATERIALIZED VIEW IF EXISTS mv_dept_dx_month;
"""


def params(conn):
    # a frequent attender: the patient at the 90th percentile of visit counts
    pid = conn.execute("""WITH visits AS (SELECT patient_id, count(*) AS n FROM appointment GROUP BY patient_id)
                          SELECT patient_id FROM visits
                          WHERE n = (SELECT percentile_disc(0.9) WITHIN GROUP (ORDER BY n) FROM visits)
                          ORDER BY patient_id LIMIT 1""").fetchone()[0]
    doc = conn.execute("""SELECT doctor_id FROM doctor WHERE department_id = 1 AND doctor_id > 12
                          ORDER BY doctor_id LIMIT 1""").fetchone()[0]
    return {"pid": pid, "doc": doc, "day": "2026-10-20", "today": "2026-10-28"}


def experiments(p):
    hist = f"""SELECT event_time, event_type, appointment_id, detail
FROM v_patient_history
WHERE patient_id = {p['pid']} AND event_time <= '{p['today']}'
ORDER BY event_time DESC"""
    day_list = """SELECT a.scheduled_start, p.mrn, p.first_name, p.last_name, a.status, a.reason
FROM appointment a JOIN patient p USING (patient_id)
WHERE a.doctor_id = {doc} AND {pred}
ORDER BY a.scheduled_start"""
    nonsarg = f"(a.scheduled_start AT TIME ZONE 'America/New_York')::date = DATE '{p['day']}'"
    sarg = (f"a.scheduled_start >= TIMESTAMPTZ '{p['day']}' "
            f"AND a.scheduled_start < TIMESTAMPTZ '{p['day']}' + interval '1 day'")
    critical = f"""SELECT r.resulted_at, p.mrn, p.last_name, t.test_name, r.result_value, d.last_name AS doctor
FROM lab_result r
JOIN lab_order lo  ON lo.lab_order_id = r.lab_order_id
JOIN appointment a ON a.appointment_id = lo.appointment_id
JOIN patient p     ON p.patient_id = a.patient_id
JOIN doctor d      ON d.doctor_id = a.doctor_id
JOIN lab_test t    ON t.test_code = r.test_code
WHERE r.abnormal_flag = 'C' AND r.resulted_at >= TIMESTAMPTZ '{p['today']}' - interval '7 days'
ORDER BY r.resulted_at DESC"""
    warfarin_base = f"""SELECT count(DISTINCT a.patient_id)
FROM prescription pr
JOIN prescription_item pi ON pi.prescription_id = pr.prescription_id
JOIN medication m ON m.medication_id = pi.medication_id AND m.generic_name = 'Warfarin'
JOIN appointment a ON a.appointment_id = pr.appointment_id
WHERE pr.issued_at >= TIMESTAMPTZ '{p['today']}' - interval '180 days'
  AND {{antijoin}}"""
    inr = (f"r.test_code = 'INR' AND r.resulted_at >= TIMESTAMPTZ '{p['today']}' - interval '90 days'")
    corr_count = f"""(SELECT count(*) FROM lab_result r
       JOIN lab_order lo ON lo.lab_order_id = r.lab_order_id
       JOIN appointment a2 ON a2.appointment_id = lo.appointment_id
       WHERE a2.patient_id = a.patient_id AND {inr}) = 0"""
    not_in = f"""a.patient_id NOT IN (SELECT a2.patient_id FROM lab_result r
       JOIN lab_order lo ON lo.lab_order_id = r.lab_order_id
       JOIN appointment a2 ON a2.appointment_id = lo.appointment_id
       WHERE {inr})"""
    not_exists = f"""NOT EXISTS (SELECT 1 FROM lab_result r
       JOIN lab_order lo ON lo.lab_order_id = r.lab_order_id
       JOIN appointment a2 ON a2.appointment_id = lo.appointment_id
       WHERE a2.patient_id = a.patient_id AND {inr})"""
    top_dx_live = """WITH counts AS (
  SELECT dpt.name AS department, dx.icd10_code, count(*) AS cases
  FROM diagnosis dx
  JOIN appointment a  ON a.appointment_id = dx.appointment_id
  JOIN doctor d       ON d.doctor_id = a.doctor_id
  JOIN department dpt ON dpt.department_id = d.department_id
  WHERE dx.diagnosis_type = 'Primary'
    AND a.scheduled_start >= '2026-01-01' AND a.scheduled_start < '2027-01-01'
  GROUP BY dpt.name, dx.icd10_code)
SELECT * FROM (SELECT *, rank() OVER (PARTITION BY department ORDER BY cases DESC) AS rnk FROM counts) r
WHERE rnk <= 3"""
    top_dx_mv = """WITH counts AS (
  SELECT department, icd10_code, sum(cases) AS cases
  FROM mv_dept_dx_month
  WHERE month >= '2026-01-01' AND month < '2027-01-01'
  GROUP BY department, icd10_code)
SELECT * FROM (SELECT *, rank() OVER (PARTITION BY department ORDER BY cases DESC) AS rnk FROM counts) r
WHERE rnk <= 3"""
    join_q = """SELECT date_trunc('month', a.scheduled_start) AS month, count(*)
FROM appointment a JOIN diagnosis dx ON dx.appointment_id = a.appointment_id
WHERE dx.diagnosis_type = 'Primary'
GROUP BY 1 ORDER BY 1"""
    prefix_q = ("SELECT mrn, first_name, last_name, date_of_birth FROM patient "
                "WHERE last_name LIKE 'Okonk%' AND date_of_birth BETWEEN '1980-01-01' AND '1984-12-31'")
    status_q = f"""SELECT status, count(*)
FROM appointment
WHERE doctor_id = {p['doc']} AND scheduled_start >= '2026-01-01' AND scheduled_start < '2027-01-01'
GROUP BY status"""

    return [
        dict(id="E1", title="Patient history (core operation, Q3)",
             technique="B-tree indexes on foreign-key columns",
             variants=[
                 dict(label="Before: FK columns unindexed", sql=hist, setup=DROP_ALL),
                 dict(label="After: 3 FK indexes", sql=hist, setup="""
CREATE INDEX ix_appt_patient_start ON appointment (patient_id, scheduled_start);
CREATE INDEX ix_prescription_appt  ON prescription (appointment_id);
CREATE INDEX ix_lab_order_appt     ON lab_order (appointment_id);
ANALYZE appointment, prescription, lab_order;""")]),
        dict(id="E2a", title="Find patient by surname prefix (Q2)",
             technique="Operator class matters (varchar_pattern_ops)",
             variants=[
                 dict(label="No index on last_name", sql=prefix_q),
                 dict(label="Default B-tree (not usable for LIKE)", sql=prefix_q,
                      setup="CREATE INDEX ix_patient_last_first ON patient (last_name, first_name);"),
                 dict(label="B-tree with varchar_pattern_ops", sql=prefix_q,
                      setup="""DROP INDEX ix_patient_last_first;
CREATE INDEX ix_patient_last_pattern ON patient (last_name varchar_pattern_ops, first_name);""")]),
        dict(id="E2b", title="Find patient by any part of surname, case-insensitive",
             technique="Trigram GIN index (pg_trgm)",
             variants=[
                 dict(label="Before: B-tree cannot serve ILIKE '%...%'",
                      sql="SELECT mrn, first_name, last_name FROM patient WHERE last_name ILIKE '%konk%'"),
                 dict(label="After: GIN trigram index",
                      sql="SELECT mrn, first_name, last_name FROM patient WHERE last_name ILIKE '%konk%'",
                      setup="""CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ix_patient_last_trgm ON patient USING gin (last_name gin_trgm_ops);""")]),
        dict(id="E3", title="Doctor's day list (Q4)",
             technique="Sargable predicate rewrite + composite index",
             variants=[
                 dict(label="Non-sargable date cast, no index", sql=day_list.format(doc=p['doc'], pred=nonsarg)),
                 dict(label="Sargable range, no index", sql=day_list.format(doc=p['doc'], pred=sarg)),
                 dict(label="Non-sargable + composite index", sql=day_list.format(doc=p['doc'], pred=nonsarg),
                      setup="CREATE INDEX ix_appt_doctor_start ON appointment (doctor_id, scheduled_start);"),
                 dict(label="Sargable + composite index", sql=day_list.format(doc=p['doc'], pred=sarg))]),
        dict(id="E4", title="Critical-result worklist, last 7 days (Q5)",
             technique="BRIN vs partial B-tree index",
             variants=[
                 dict(label="No index on resulted_at", sql=critical),
                 dict(label="BRIN on resulted_at", sql=critical,
                      setup="CREATE INDEX ix_lab_result_resulted_brin ON lab_result USING brin (resulted_at) WITH (pages_per_range = 32);"),
                 dict(label="Partial B-tree WHERE flag = 'C'", sql=critical,
                      setup="""DROP INDEX ix_lab_result_resulted_brin;
CREATE INDEX ix_lab_result_critical ON lab_result (resulted_at) WHERE abnormal_flag = 'C';""")],
             sizes=["ix_lab_result_resulted_brin", "ix_lab_result_critical"]),
        dict(id="E5", title="Warfarin patients with no recent INR (Q8b)",
             technique="Anti-join rewrite",
             variants=[
                 dict(label="Correlated COUNT(*) = 0", sql=warfarin_base.format(antijoin=corr_count)),
                 dict(label="NOT IN (subquery)", sql=warfarin_base.format(antijoin=not_in)),
                 dict(label="NOT EXISTS (anti-join)", sql=warfarin_base.format(antijoin=not_exists))]),
        dict(id="E6", title="Top-3 diagnoses per department, 2026 (Q6)",
             technique="Materialised view (pre-aggregation)",
             variants=[
                 dict(label="Live aggregation over 590k diagnoses", sql=top_dx_live),
                 dict(label="Query the materialised view", sql=top_dx_mv, setup="""
CREATE MATERIALIZED VIEW mv_dept_dx_month AS
SELECT dpt.name AS department, date_trunc('month', a.scheduled_start)::date AS month,
       dx.icd10_code, count(*) AS cases
FROM diagnosis dx
JOIN appointment a  ON a.appointment_id = dx.appointment_id
JOIN doctor d       ON d.doctor_id = a.doctor_id
JOIN department dpt ON dpt.department_id = d.department_id
WHERE dx.diagnosis_type = 'Primary'
GROUP BY 1, 2, 3;
CREATE UNIQUE INDEX ux_mv_dept_dx_month ON mv_dept_dx_month (department, month, icd10_code);
ANALYZE mv_dept_dx_month;""")],
             extra="refresh"),
        dict(id="E7", title="Join algorithm choice: appointment JOIN diagnosis (590k x 590k)",
             technique="Cost-based optimiser vs forced strategies",
             variants=[
                 dict(label="Planner's choice", sql=join_q),
                 dict(label="Forced merge join", sql=join_q,
                      session=["SET enable_hashjoin = off"]),
                 dict(label="Forced nested loop", sql=join_q,
                      session=["SET enable_hashjoin = off", "SET enable_mergejoin = off"]),
                 dict(label="Planner's choice, random_page_cost = 1.1 (SSD)", sql=join_q,
                      session=["SET random_page_cost = 1.1"])]),
        dict(id="E8", title="Status counts for one doctor, 2026",
             technique="Covering index (INCLUDE) enables index-only scan",
             variants=[
                 dict(label="Composite index, heap lookups", sql=status_q),
                 dict(label="Covering index INCLUDE (status)", sql=status_q, setup="""
DROP INDEX ix_appt_doctor_start;
CREATE INDEX ix_appt_doctor_start_cov ON appointment (doctor_id, scheduled_start) INCLUDE (status, patient_id);
VACUUM (ANALYZE) appointment;""")]),
    ]


def plan_stats(plan_json):
    root = plan_json[0]["Plan"]

    def walk(n, acc):
        acc.append(n["Node Type"] + (f" on {n['Relation Name']}" if "Relation Name" in n else "")
                   + (f" using {n['Index Name']}" if "Index Name" in n else ""))
        for c in n.get("Plans", []):
            walk(c, acc)
        return acc
    nodes = walk(root, [])
    scans = [n for n in nodes if "Scan" in n]
    return dict(exec_ms=plan_json[0]["Execution Time"], plan_ms=plan_json[0]["Planning Time"],
                hit=root.get("Shared Hit Blocks", 0), read=root.get("Shared Read Blocks", 0),
                rows=root.get("Actual Rows"), top=root["Node Type"], scans=scans)


def exec_script(conn, sql):
    """Run a multi-statement script one statement at a time (VACUUM cannot run in a batch)."""
    for stmt in re.split(r";\s*(?:\n|$)", sql):
        if stmt.strip():
            conn.execute(stmt)


def run_variant(conn, exp, idx, v):
    for s in SESSION + v.get("session", []):
        conn.execute(s)
    if v.get("setup"):
        exec_script(conn, v["setup"])
    conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {v['sql']}")       # warm-up
    samples = [plan_stats(conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {v['sql']}").fetchone()[0])
               for _ in range(RUNS)]
    text = "\n".join(r[0] for r in conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE OFF) {v['sql']}"))
    slug = re.sub(r"[^a-z0-9]+", "_", v["label"].lower()).strip("_")[:40]
    fname = f"{exp['id']}_{idx + 1}_{slug}.txt"
    with open(os.path.join(PLANS, fname), "w") as f:
        f.write(f"-- {exp['id']} {exp['title']}\n-- Variant {idx + 1}: {v['label']}\n")
        if v.get("session"):
            f.write("-- Session settings: " + "; ".join(v["session"]) + "\n")
        f.write(f"-- Median execution time over {RUNS} runs: {statistics.median(s['exec_ms'] for s in samples):.3f} ms\n\n")
        f.write(v["sql"] + ";\n\n" + text + "\n")
    for s in ["RESET enable_hashjoin", "RESET enable_mergejoin", "RESET random_page_cost"]:
        conn.execute(s)
    med = statistics.median(s["exec_ms"] for s in samples)
    return dict(label=v["label"], median_ms=round(med, 3), min_ms=round(min(s["exec_ms"] for s in samples), 3),
                max_ms=round(max(s["exec_ms"] for s in samples), 3),
                planning_ms=round(statistics.median(s["plan_ms"] for s in samples), 3),
                shared_hit=samples[-1]["hit"], shared_read=samples[-1]["read"], rows=samples[-1]["rows"],
                scans=samples[-1]["scans"], plan_file=fname)


def write_cost(conn):
    """E9: cost of index maintenance on the write path (100 000 inserts)."""
    out = []
    configs = [
        ("Primary key only", ""),
        ("+ 3 B-tree indexes", """
CREATE INDEX ON bench_appt (patient_id, scheduled_start);
CREATE INDEX ON bench_appt (doctor_id, scheduled_start) INCLUDE (status, patient_id);
CREATE INDEX ON bench_appt (status);"""),
        ("+ 3 B-tree + 2 GiST exclusion", """
ALTER TABLE bench_appt ADD EXCLUDE USING gist (doctor_id WITH =, tstzrange(scheduled_start, scheduled_end) WITH &&)
      WHERE (status <> 'Cancelled');
ALTER TABLE bench_appt ADD EXCLUDE USING gist (patient_id WITH =, tstzrange(scheduled_start, scheduled_end) WITH &&)
      WHERE (status <> 'Cancelled');"""),
    ]
    conn.execute("DROP TABLE IF EXISTS bench_appt")
    conn.execute("""CREATE TABLE bench_appt (id bigint PRIMARY KEY, patient_id int, doctor_id int,
                    scheduled_start timestamptz, scheduled_end timestamptz,
                    status appointment_status, reason varchar(200))""")
    for label, ddl in configs:
        if ddl:
            exec_script(conn, ddl)
        times = []
        for _ in range(3):
            conn.execute("TRUNCATE bench_appt")
            t0 = time.perf_counter()
            conn.execute("""INSERT INTO bench_appt
                            SELECT appointment_id, patient_id, doctor_id, scheduled_start, scheduled_end, status, reason
                            FROM appointment ORDER BY appointment_id LIMIT 100000""")
            times.append((time.perf_counter() - t0) * 1000)
        size = conn.execute("SELECT pg_indexes_size('bench_appt')").fetchone()[0]
        out.append(dict(label=label, median_ms=round(statistics.median(times), 1), index_bytes=size))
    conn.execute("DROP TABLE bench_appt")
    return out


def main():
    os.makedirs(PLANS, exist_ok=True)
    os.makedirs(RESULTS, exist_ok=True)
    with psycopg.connect(DSN, autocommit=True) as conn:
        for s in SESSION:
            conn.execute(s)
        p = params(conn)
        exec_script(conn, DROP_ALL)
        conn.execute("VACUUM (ANALYZE)")
        env = dict(server=conn.execute("SELECT version()").fetchone()[0],
                   cpus=os.cpu_count(), run_at=datetime.now().isoformat(timespec="seconds"),
                   settings={k: conn.execute(f"SHOW {k}").fetchone()[0] for k in
                             ["shared_buffers", "work_mem", "effective_cache_size", "random_page_cost",
                              "max_parallel_workers_per_gather", "jit"]},
                   rows={t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                         ["patient", "doctor", "appointment", "diagnosis", "prescription",
                          "prescription_item", "lab_order", "lab_result"]},
                   params=p)
        results = []
        for exp in experiments(p):
            print(f"== {exp['id']} {exp['title']}")
            vres = []
            for i, v in enumerate(exp["variants"]):
                r = run_variant(conn, exp, i, v)
                print(f"   {r['label']:<45} {r['median_ms']:>10.3f} ms  {r['scans']}")
                vres.append(r)
            entry = dict(id=exp["id"], title=exp["title"], technique=exp["technique"], variants=vres)
            if exp.get("sizes"):
                entry["index_sizes"] = {}
                for ix in exp["sizes"]:
                    row = conn.execute("SELECT pg_relation_size(to_regclass(%s))", (ix,)).fetchone()[0]
                    entry["index_sizes"][ix] = row
            if exp.get("extra") == "refresh":
                t = []
                for _ in range(3):
                    t0 = time.perf_counter()
                    conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_dept_dx_month")
                    t.append((time.perf_counter() - t0) * 1000)
                entry["refresh_concurrently_ms"] = round(statistics.median(t), 1)
            results.append(entry)
        # BRIN was dropped inside E4; re-measure its size for the record
        conn.execute("CREATE INDEX ix_lab_result_resulted_brin ON lab_result USING brin (resulted_at) WITH (pages_per_range = 32)")
        e4 = next(r for r in results if r["id"] == "E4")
        e4["index_sizes"]["ix_lab_result_resulted_brin"] = conn.execute(
            "SELECT pg_relation_size('ix_lab_result_resulted_brin')").fetchone()[0]
        e4["index_sizes"]["lab_result (table)"] = conn.execute("SELECT pg_relation_size('lab_result')").fetchone()[0]
        conn.execute("DROP INDEX ix_lab_result_resulted_brin")
        print("== E9 write cost")
        wc = write_cost(conn)
        for w in wc:
            print(f"   {w['label']:<35} {w['median_ms']:>8.1f} ms  indexes {w['index_bytes'] / 1e6:.1f} MB")
        results.append(dict(id="E9", title="Write cost of indexes: inserting 100 000 appointments",
                            technique="Index maintenance overhead", write_cost=wc))

    with open(os.path.join(RESULTS, "benchmark_results.json"), "w") as f:
        json.dump(dict(environment=env, experiments=results), f, indent=2, default=str)

    lines = ["# Benchmark summary", "",
             f"Median of {RUNS} `EXPLAIN (ANALYZE, BUFFERS)` executions after one warm-up run (warm cache). "
             f"PostgreSQL {env['server'].split()[1]}, {env['cpus']} vCPU, shared_buffers {env['settings']['shared_buffers']}, "
             f"work_mem {env['settings']['work_mem']}, JIT off. Run at {env['run_at']}.", "",
             "| Exp | Variant | Median ms | vs first | Buffers hit/read | Access path |", "|---|---|---|---|---|---|"]
    for e in results:
        if "variants" not in e:
            continue
        base = e["variants"][0]["median_ms"]
        for i, v in enumerate(e["variants"]):
            speed = "—" if i == 0 else f"{base / v['median_ms']:.1f}x faster" if v["median_ms"] < base else f"{v['median_ms'] / base:.1f}x slower"
            path = "; ".join(dict.fromkeys(v["scans"]))[:120]
            lines.append(f"| {e['id'] if i == 0 else ''} | {v['label']} | {v['median_ms']:.3f} | {speed} | "
                         f"{v['shared_hit']}/{v['shared_read']} | {path} |")
    e9 = results[-1]["write_cost"]
    lines += ["", "**E9 — write cost (100 000-row insert, median of 3)**", "", "| Configuration | Insert ms | Index size |", "|---|---|---|"]
    lines += [f"| {w['label']} | {w['median_ms']:.0f} | {w['index_bytes'] / 1e6:.1f} MB |" for w in e9]
    with open(os.path.join(RESULTS, "benchmark_summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("done")


if __name__ == "__main__":
    main()
