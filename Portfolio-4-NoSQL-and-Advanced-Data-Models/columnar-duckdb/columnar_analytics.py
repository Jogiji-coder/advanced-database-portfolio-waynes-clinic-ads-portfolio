"""
Portfolio 4 - columnar storage for analytics (DuckDB 1.x/2.x + Parquet), executed.

Row stores (PostgreSQL, MongoDB) keep each record together - ideal for "give me
this patient". Analytical questions ("abnormal-result rate per test per month
over two years") touch a few columns of every row, which a column store reads
far more cheaply. This script:
  1. extracts a denormalised lab-results fact table from PostgreSQL,
  2. stores it as a ZSTD-compressed Parquet file and shows per-column sizes,
  3. runs the same analytical query on PostgreSQL (row store) and DuckDB (column store).

    python columnar_analytics.py      -> ../evidence/columnar_duckdb_output.txt
"""
import os
import statistics
import time

import duckdb
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
EVID = os.path.join(HERE, "..", "evidence")
PG = os.environ.get("WC_DSN", "host=localhost dbname=waynes_clinic user=postgres password=postgres")
OUT = []


def say(t=""):
    print(t)
    OUT.append(t)


FACT_SQL = """
SELECT r.lab_result_id, r.resulted_at, r.test_code, r.result_value, r.abnormal_flag,
       lo.priority, dpt.name AS department, a.doctor_id, a.patient_id, p.sex,
       extract(year FROM p.date_of_birth)::int AS birth_year
FROM clinic.lab_result r
JOIN clinic.lab_order lo  ON lo.lab_order_id = r.lab_order_id
JOIN clinic.appointment a ON a.appointment_id = lo.appointment_id
JOIN clinic.doctor d      ON d.doctor_id = a.doctor_id
JOIN clinic.department dpt ON dpt.department_id = d.department_id
JOIN clinic.patient p     ON p.patient_id = a.patient_id"""

PG_QUERY = """
SELECT date_trunc('month', r.resulted_at) AS month, r.test_code, dpt.name AS department,
       count(*) AS results,
       round(100.0 * count(*) FILTER (WHERE r.abnormal_flag <> 'N') / count(*), 1) AS abnormal_pct,
       round(avg(r.result_value), 2) AS mean_value
FROM clinic.lab_result r
JOIN clinic.lab_order lo  ON lo.lab_order_id = r.lab_order_id
JOIN clinic.appointment a ON a.appointment_id = lo.appointment_id
JOIN clinic.doctor d      ON d.doctor_id = a.doctor_id
JOIN clinic.department dpt ON dpt.department_id = d.department_id
GROUP BY 1, 2, 3"""

DUCK_QUERY = """
SELECT date_trunc('month', resulted_at)::DATE AS month, test_code, department,
       count(*) AS results,
       round(100.0 * count(*) FILTER (WHERE abnormal_flag <> 'N') / count(*), 1) AS abnormal_pct,
       round(avg(result_value), 2) AS mean_value
FROM read_parquet('{path}')
GROUP BY ALL"""


def timed(fn, runs=5):
    fn()
    t = []
    for _ in range(runs):
        t0 = time.perf_counter(); fn(); t.append((time.perf_counter() - t0) * 1000)
    return statistics.median(t)


def main():
    os.makedirs(DATA, exist_ok=True)
    csv_path = os.path.join(DATA, "lab_fact.csv")
    pq_path = os.path.join(DATA, "lab_fact.parquet")
    with psycopg.connect(PG) as pg:
        pg.execute("SET timezone TO 'UTC'")
        with open(csv_path, "wb") as f, pg.cursor().copy(f"COPY ({FACT_SQL}) TO STDOUT WITH (FORMAT csv, HEADER)") as cp:
            for chunk in cp:
                f.write(chunk)
        pg_table_mb = pg.execute("SELECT pg_total_relation_size('clinic.lab_result') / 1e6").fetchone()[0]
        pg_ms = timed(lambda: pg.execute(PG_QUERY).fetchall())
        pg_rows = len(pg.execute(PG_QUERY).fetchall())
        # fair comparison: the SAME denormalised table, but stored row-wise in PostgreSQL
        pg.execute(f"CREATE TEMP TABLE lab_fact AS {FACT_SQL}")
        pg.execute("ANALYZE lab_fact")
        flat_q = """SELECT date_trunc('month', resulted_at)::date AS month, test_code, department, count(*),
                    round(100.0 * count(*) FILTER (WHERE abnormal_flag <> 'N') / count(*), 1), round(avg(result_value), 2)
                    FROM lab_fact GROUP BY 1, 2, 3"""
        pg_flat_ms = timed(lambda: pg.execute(flat_q).fetchall())

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")              # same month boundaries as the PostgreSQL session
    con.execute(f"""COPY (SELECT * FROM read_csv_auto('{csv_path}', timestampformat='%Y-%m-%d %H:%M:%S%z'))
                    TO '{pq_path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 100000)""")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{pq_path}')").fetchone()[0]
    say(f"Fact table: {n:,} lab results x 11 columns")
    say(f"  CSV extract          {os.path.getsize(csv_path) / 1e6:7.1f} MB")
    say(f"  Parquet (ZSTD)       {os.path.getsize(pq_path) / 1e6:7.1f} MB")
    say(f"  PostgreSQL lab_result table incl. indexes (fewer columns, normalised): {pg_table_mb:.1f} MB")
    say("")
    say("Per-column compressed size inside the Parquet file (columnar storage + encoding):")
    for col, comp, uncomp in con.execute(f"""
            SELECT path_in_schema, sum(total_compressed_size), sum(total_uncompressed_size)
            FROM parquet_metadata('{pq_path}') GROUP BY 1 ORDER BY 2 DESC""").fetchall():
        say(f"  {col:<15} {comp / 1024:8.0f} KB   (uncompressed {uncomp / 1024:7.0f} KB, ratio {uncomp / max(comp, 1):4.1f}x)")

    duck_q = DUCK_QUERY.format(path=pq_path)
    duck_ms = timed(lambda: con.execute(duck_q).fetchall())
    duck_rows = len(con.execute(duck_q).fetchall())
    say("")
    say("Analytical query: monthly result count, abnormal % and mean value per test per department (2 years)")
    say(f"  PostgreSQL, normalised row store (5-table join): {pg_ms:8.1f} ms  -> {pg_rows} groups")
    say(f"  PostgreSQL, same denormalised table (row store):{pg_flat_ms:8.1f} ms")
    say(f"  DuckDB on Parquet (columnar, vectorised):       {duck_ms:8.1f} ms  -> {duck_rows} groups")
    say(f"  columnar vs row store on identical data: {pg_flat_ms / duck_ms:.1f}x; vs the normalised operational schema: {pg_ms / duck_ms:.1f}x")
    say("")
    say("DuckDB plan - note the Parquet scan reads only the 5 projected columns of 11:")
    plan = con.execute("EXPLAIN ANALYZE " + duck_q).fetchall()[0][1]
    keep = [l for l in plan.splitlines() if l.strip() and not set(l.strip()) <= set("─│┌┐└┘┬┴├┤ ")]
    for l in keep[:60]:
        say("  " + l.rstrip())
    say("")
    say("Sample output (General Medicine, HbA1c):")
    for r in con.execute(duck_q.replace("GROUP BY ALL", "GROUP BY ALL") +
                         " HAVING test_code = 'HBA1C' AND department = 'General Medicine' ORDER BY month LIMIT 6").fetchall():
        say("  " + str(r[0])[:7] + "  " + "  ".join(str(x) for x in r[1:]))
    open(os.path.join(EVID, "columnar_duckdb_output.txt"), "w").write("\n".join(OUT) + "\n")
    os.remove(csv_path)


if __name__ == "__main__":
    main()
