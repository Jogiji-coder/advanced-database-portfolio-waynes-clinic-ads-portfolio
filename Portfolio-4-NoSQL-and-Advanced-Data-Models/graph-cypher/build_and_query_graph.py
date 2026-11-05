"""
Portfolio 4 - graph model. Builds a property graph of the clinic's last three
months (Aug-Oct 2026) in Kùzu from PostgreSQL, then runs graph queries in
Cypher. A relational version of the contact-tracing and shortest-path queries is
timed for comparison.

    python build_and_query_graph.py
Writes ../evidence/graph_queries_output.txt and graph_vs_sql_timing.json
"""
import json
import os
import shutil
import time

import kuzu
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")              # regenerated CSV extracts (git-ignored)
DBDIR = os.path.join(HERE, "kuzu_db")          # regenerated database (git-ignored)
EVID = os.path.join(HERE, "..", "evidence")
PG = os.environ.get("WC_DSN", "host=localhost dbname=waynes_clinic user=postgres password=postgres")
WINDOW = ("2026-08-01", "2026-10-28")
OUT = []


def say(t=""):
    print(t)
    OUT.append(t)


EXTRACTS = {
    "patients.csv": f"""SELECT DISTINCT p.patient_id, p.mrn, p.first_name || ' ' || p.last_name,
                               extract(year FROM p.date_of_birth)::int, p.sex
                        FROM clinic.patient p JOIN clinic.appointment a USING (patient_id)
                        WHERE a.status = 'Completed' AND a.scheduled_start >= '{WINDOW[0]}' AND a.scheduled_start < '{WINDOW[1]}'""",
    "doctors.csv": """SELECT d.doctor_id, 'Dr ' || d.first_name || ' ' || d.last_name, s.name
                      FROM clinic.doctor d JOIN clinic.specialty s USING (specialty_id)""",
    "departments.csv": "SELECT name FROM clinic.department",
    "conditions.csv": "SELECT icd10_code, description FROM clinic.icd10_code",
    "medications.csv": "SELECT medication_id, generic_name FROM clinic.medication",
    "works_in.csv": """SELECT d.doctor_id, dp.name FROM clinic.doctor d JOIN clinic.department dp USING (department_id)""",
    "visited.csv": f"""SELECT a.patient_id, a.doctor_id, a.appointment_id,
                              (a.scheduled_start AT TIME ZONE 'America/New_York')::date,
                              (a.scheduled_start AT TIME ZONE 'America/New_York')
                       FROM clinic.appointment a
                       WHERE a.status = 'Completed' AND a.scheduled_start >= '{WINDOW[0]}' AND a.scheduled_start < '{WINDOW[1]}'""",
    "diagnosed.csv": f"""SELECT a.patient_id, dx.icd10_code, (a.scheduled_start AT TIME ZONE 'America/New_York')::date,
                                dx.diagnosis_type, a.appointment_id
                         FROM clinic.diagnosis dx JOIN clinic.appointment a USING (appointment_id)
                         WHERE a.status = 'Completed' AND a.scheduled_start >= '{WINDOW[0]}' AND a.scheduled_start < '{WINDOW[1]}'""",
    "prescribed.csv": f"""SELECT a.patient_id, pi.medication_id, (pr.issued_at AT TIME ZONE 'America/New_York')::date, a.appointment_id
                          FROM clinic.prescription_item pi JOIN clinic.prescription pr USING (prescription_id)
                          JOIN clinic.appointment a USING (appointment_id)
                          WHERE a.status = 'Completed' AND a.scheduled_start >= '{WINDOW[0]}' AND a.scheduled_start < '{WINDOW[1]}'""",
    "allergic.csv": f"""SELECT pa.patient_id, pa.medication_id, pa.severity FROM clinic.patient_allergy pa
                        WHERE pa.patient_id IN (SELECT patient_id FROM clinic.appointment
                              WHERE status = 'Completed' AND scheduled_start >= '{WINDOW[0]}' AND scheduled_start < '{WINDOW[1]}')""",
}


def export():
    shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA)
    with psycopg.connect(PG) as conn:
        for fname, sql in EXTRACTS.items():
            with open(os.path.join(DATA, fname), "wb") as f, \
                    conn.cursor().copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv)") as cp:
                for chunk in cp:
                    f.write(chunk)
        meds = dict((name, mid) for mid, name in conn.execute("SELECT medication_id, generic_name FROM clinic.medication"))
    with open(os.path.join(HERE, "interactions.csv")) as src, open(os.path.join(DATA, "interacts.csv"), "w") as dst:
        next(src)
        for line in src:
            a, b, sev, eff = line.rstrip("\n").split(",", 3)
            dst.write(f'{meds[a]},{meds[b]},{sev},"{eff}"\n')


def build():
    for path in (DBDIR, DBDIR + ".wal"):             # Kuzu 0.11 stores the database as a single file
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
    db = kuzu.Database(DBDIR)
    conn = kuzu.Connection(db)
    ddl = "\n".join(l for l in open(os.path.join(HERE, "schema.cypher")).read().splitlines()
                    if not l.strip().startswith("//"))           # strip comments first
    for stmt in ddl.split(";"):
        if stmt.strip():
            conn.execute(stmt.strip())
    t0 = time.perf_counter()
    for table, f in [("Patient", "patients"), ("Doctor", "doctors"), ("Department", "departments"),
                     ("Condition", "conditions"), ("Medication", "medications"), ("WORKS_IN", "works_in"),
                     ("VISITED", "visited"), ("DIAGNOSED_WITH", "diagnosed"), ("PRESCRIBED", "prescribed"),
                     ("ALLERGIC_TO", "allergic"), ("INTERACTS_WITH", "interacts")]:
        conn.execute(f'COPY {table} FROM "{os.path.join(DATA, f + ".csv")}" (header=false)')
    say(f"Graph loaded in {time.perf_counter() - t0:.1f} s")
    for label in ["Patient", "Doctor", "Department", "Condition", "Medication"]:
        n = conn.execute(f"MATCH (n:{label}) RETURN count(n)").get_next()[0]
        say(f"  nodes  {label:<15} {n:>8,}")
    for rel in ["WORKS_IN", "VISITED", "DIAGNOSED_WITH", "PRESCRIBED", "ALLERGIC_TO", "INTERACTS_WITH"]:
        n = conn.execute(f"MATCH ()-[r:{rel}]->() RETURN count(r)").get_next()[0]
        say(f"  edges  {rel:<15} {n:>8,}")
    return db, conn


def run(conn, title, cypher, limit=12):
    say("")
    say("=" * 100)
    say(title)
    say("-" * 100)
    say(cypher.strip())
    say("-" * 100)
    t0 = time.perf_counter()
    res = conn.execute(cypher)
    rows = []
    while res.has_next():
        rows.append(res.get_next())
    ms = (time.perf_counter() - t0) * 1000
    cols = res.get_column_names()
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows[:limit])) if rows else len(str(c)) for i, c in enumerate(cols)]
    widths = [min(w, 72) for w in widths]
    say(" | ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    say("-+-".join("-" * w for w in widths))
    for r in rows[:limit]:
        say(" | ".join(str(v)[:72].ljust(w) for v, w in zip(r, widths)))
    say(f"({len(rows)} rows, {ms:.1f} ms)")
    return rows, ms


def main():
    export()
    db, conn = build()
    timings = {}

    run(conn, "G1  MEDICATION SAFETY - patients prescribed both drugs of a MAJOR interacting pair (Aug-Oct 2026)", """
MATCH (p:Patient)-[:PRESCRIBED]->(m1:Medication)-[i:INTERACTS_WITH]->(m2:Medication)<-[:PRESCRIBED]-(p)
WHERE i.severity = 'Major'
RETURN m1.name AS drug_1, m2.name AS drug_2, i.effect AS risk, count(DISTINCT p) AS patients
ORDER BY patients DESC""")

    run(conn, "G1b One patient's interaction alerts (any severity), with when each drug was prescribed", """
MATCH (p:Patient)-[r1:PRESCRIBED]->(m1:Medication)-[i:INTERACTS_WITH]->(m2:Medication)<-[r2:PRESCRIBED]-(p)
WITH p, count(i) AS alerts
ORDER BY alerts DESC, p.id LIMIT 1
MATCH (p)-[r1:PRESCRIBED]->(m1:Medication)-[i:INTERACTS_WITH]->(m2:Medication)<-[r2:PRESCRIBED]-(p)
RETURN p.mrn AS mrn, p.name AS patient, m1.name AS drug_1, r1.day AS rx_1, m2.name AS drug_2, r2.day AS rx_2,
       i.severity AS severity
ORDER BY severity, rx_2""")

    rows, ms = run(conn, "G2  INFECTION CONTROL - contact tracing: who shared a clinic (same doctor, same day) with a COVID-19 patient?", """
MATCH (c:Patient)-[d:DIAGNOSED_WITH]->(:Condition {code: 'U07.1'}),
      (c)-[v1:VISITED]->(doc:Doctor)<-[v2:VISITED]-(o:Patient)
WHERE v1.appointment_id = d.appointment_id AND v2.day = v1.day AND o.id <> c.id
RETURN count(DISTINCT c) AS covid_cases, count(DISTINCT o) AS exposed_patients, count(*) AS exposure_events""")
    timings["G2_contact_tracing_graph_ms"] = round(ms, 1)

    run(conn, "G2b Exposed patients who were themselves diagnosed with COVID-19 within 14 days (possible transmission)", """
MATCH (c:Patient)-[d:DIAGNOSED_WITH]->(cov:Condition {code: 'U07.1'}),
      (c)-[v1:VISITED]->(doc:Doctor)<-[v2:VISITED]-(o:Patient)-[d2:DIAGNOSED_WITH]->(cov)
WHERE v1.appointment_id = d.appointment_id AND v2.day = v1.day AND o.id <> c.id
  AND d2.day > d.day AND d2.day <= d.day + INTERVAL('14 days')
RETURN c.mrn AS index_case, d.day AS exposure_day, doc.name AS clinic_of, o.mrn AS contact, d2.day AS contact_diagnosed
ORDER BY exposure_day""")

    run(conn, "G3  CARE COORDINATION - doctor pairs who share the most patients (referral network)", """
MATCH (d1:Doctor)<-[:VISITED]-(p:Patient)-[:VISITED]->(d2:Doctor)
WHERE d1.id < d2.id
RETURN d1.name AS doctor_1, d1.specialty AS specialty_1, d2.name AS doctor_2, d2.specialty AS specialty_2,
       count(DISTINCT p) AS shared_patients
ORDER BY shared_patients DESC LIMIT 8""")

    run(conn, "G4  POPULATION HEALTH - comorbidity: conditions most often found in the same patient", """
MATCH (c1:Condition)<-[:DIAGNOSED_WITH]-(p:Patient)-[:DIAGNOSED_WITH]->(c2:Condition)
WHERE c1.code < c2.code
RETURN c1.code AS code_1, c1.description AS condition_1, c2.code AS code_2, c2.description AS condition_2,
       count(DISTINCT p) AS patients
ORDER BY patients DESC LIMIT 8""")

    rows, ms = run(conn, "G5  VARIABLE-LENGTH PATHS - shortest chain of shared clinicians linking two patients", """
MATCH (a:Patient {mrn: 'WCB0000003'}), (b:Patient {mrn: 'WCB0000016'})
MATCH (a)-[e:VISITED* SHORTEST 1..8]-(b)
RETURN length(e) AS hops, a.name AS from_patient, properties(nodes(e), 'name') AS via, b.name AS to_patient""")
    timings["G5_shortest_path_graph_ms"] = round(ms, 1)

    run(conn, "G6  SIMILAR PATIENTS - who shares the most conditions with Tyrone Jackson (WCB0000003)?", """
MATCH (t:Patient {mrn: 'WCB0000003'})-[:DIAGNOSED_WITH]->(c:Condition)<-[:DIAGNOSED_WITH]-(o:Patient)
WHERE o.id <> t.id
RETURN o.mrn AS patient, o.name AS name, collect(DISTINCT c.code) AS shared_conditions, count(DISTINCT c) AS n
ORDER BY n DESC, patient LIMIT 6""")

    # ---- the same contact-tracing question in SQL, for comparison
    sql_ct = f"""
SELECT count(DISTINCT c.patient_id) AS covid_cases, count(DISTINCT o.patient_id) AS exposed_patients, count(*) AS exposure_events
FROM clinic.diagnosis dx
JOIN clinic.appointment c ON c.appointment_id = dx.appointment_id
JOIN clinic.appointment o ON o.doctor_id = c.doctor_id
     AND (o.scheduled_start AT TIME ZONE 'America/New_York')::date = (c.scheduled_start AT TIME ZONE 'America/New_York')::date
     AND o.patient_id <> c.patient_id AND o.status = 'Completed'
     AND o.scheduled_start >= '{WINDOW[0]}' AND o.scheduled_start < '{WINDOW[1]}'
WHERE dx.icd10_code = 'U07.1' AND c.status = 'Completed'
  AND c.scheduled_start >= '{WINDOW[0]}' AND c.scheduled_start < '{WINDOW[1]}'"""
    with psycopg.connect(PG) as pg:
        pg.execute(sql_ct)
        t0 = time.perf_counter()
        r = pg.execute(sql_ct).fetchone()
        timings["G2_contact_tracing_sql_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    say("")
    say("=" * 100)
    say("Same contact-tracing question in SQL (PostgreSQL self-join on appointment):")
    say(sql_ct.strip())
    say(f"-> covid_cases={r[0]}, exposed_patients={r[1]}, exposure_events={r[2]}  ({timings['G2_contact_tracing_sql_ms']} ms)")
    say("A variable-length path such as G5 needs a WITH RECURSIVE query in SQL that alternates patient and")
    say("doctor joins and must track visited nodes to stop cycles - see shortest_path_recursive.sql.")

    sp_sql = open(os.path.join(HERE, "shortest_path_recursive.sql")).read()
    with psycopg.connect(PG) as pg:
        pg.execute(sp_sql)
        t0 = time.perf_counter()
        hops = pg.execute(sp_sql).fetchone()[0]
        timings["G5_shortest_path_sql_recursive_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    say(f"SQL WITH RECURSIVE breadth-first search (shortest_path_recursive.sql) -> hops = {hops} "
        f"({timings['G5_shortest_path_sql_recursive_ms']} ms), distance only, no path.")

    open(os.path.join(EVID, "graph_queries_output.txt"), "w").write("\n".join(OUT) + "\n")
    json.dump(timings, open(os.path.join(EVID, "graph_vs_sql_timing.json"), "w"), indent=2)
    print(json.dumps(timings, indent=2))


if __name__ == "__main__":
    main()
