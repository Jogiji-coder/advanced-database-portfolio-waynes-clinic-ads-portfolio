-- =====================================================================
-- Wayne's Clinic Baltimore — Advanced Database Systems Portfolio
-- Portfolio 2 · Script 03 · Recommended physical design (applied after benchmarking)
--
-- Each index below earned its place in scripts/benchmark.py. Indexes that
-- were tested and NOT adopted are listed at the end with the reason.
-- Run:  psql -d waynes_clinic -f 03_recommended_indexes.sql
-- =====================================================================
\set ON_ERROR_STOP 1
SET search_path TO clinic, public;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- E1  Patient history: the three FK columns on the history path
CREATE INDEX IF NOT EXISTS ix_appt_patient_start ON appointment  (patient_id, scheduled_start);
CREATE INDEX IF NOT EXISTS ix_prescription_appt  ON prescription (appointment_id);
CREATE INDEX IF NOT EXISTS ix_lab_order_appt     ON lab_order    (appointment_id);

-- E2  Reception search: prefix (pattern-ops B-tree) and infix/case-insensitive (trigram).
--     A default B-tree cannot serve LIKE 'abc%' because the database collation is C.UTF-8,
--     not "C"; varchar_pattern_ops compares character by character, so it can.
CREATE INDEX IF NOT EXISTS ix_patient_last_pattern ON patient (last_name varchar_pattern_ops, first_name);
CREATE INDEX IF NOT EXISTS ix_patient_last_trgm  ON patient USING gin (last_name gin_trgm_ops);

-- E3 + E8  Doctor day list and per-doctor counts: one covering composite index
DROP INDEX IF EXISTS ix_appt_doctor_start;
CREATE INDEX IF NOT EXISTS ix_appt_doctor_start_cov
    ON appointment (doctor_id, scheduled_start) INCLUDE (status, patient_id);

-- E4  Critical-result worklist: small partial index over ~0.7 % of rows (2,853 of 401,731)
CREATE INDEX IF NOT EXISTS ix_lab_result_critical
    ON lab_result (resulted_at) WHERE abnormal_flag = 'C';

-- E6  Management dashboard: pre-aggregated monthly diagnosis counts
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_dept_dx_month AS
SELECT dpt.name AS department, date_trunc('month', a.scheduled_start)::date AS month,
       dx.icd10_code, count(*) AS cases
FROM diagnosis dx
JOIN appointment a  ON a.appointment_id = dx.appointment_id
JOIN doctor d       ON d.doctor_id = a.doctor_id
JOIN department dpt ON dpt.department_id = d.department_id
WHERE dx.diagnosis_type = 'Primary'
GROUP BY 1, 2, 3;
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_dept_dx_month ON mv_dept_dx_month (department, month, icd10_code);
-- Refreshed nightly without blocking readers:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY clinic.mv_dept_dx_month;

VACUUM (ANALYZE) appointment, patient, prescription, lab_order, lab_result;

-- Not adopted:
--   BRIN on lab_result(resulted_at): tiny, but for the 7-day critical worklist the
--     partial B-tree is faster; revisit if lab_result grows to hundreds of millions of rows.
--   Plain (doctor_id, scheduled_start): superseded by the covering version above.
--   Default B-tree on patient(last_name): ignored by the planner for LIKE (see E2a).
--   Index on appointment(status): low selectivity (5 values) - the planner prefers a scan.

\pset footer off
\echo 'Secondary indexes now in place on the hot tables:'
SELECT c.relname AS table_name, i.relname AS index_name, am.amname AS method,
       pg_size_pretty(pg_relation_size(i.oid)) AS size
FROM pg_index x
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_am am   ON am.oid = i.relam
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'clinic' AND i.relname LIKE 'ix_%'
  AND c.relname IN ('appointment','patient','prescription','lab_order','lab_result')
ORDER BY c.relname, i.relname;
