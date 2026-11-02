-- =====================================================================
-- Wayne's Clinic Baltimore — Advanced Database Systems Portfolio
-- Portfolio 2 · Script 02 · Business queries (run on the bulk-loaded data)
--
-- Every query answers a question a real role at the clinic asks. Values
-- such as the patient or doctor are chosen at the top with \gset so the
-- script runs on any reload of the data. "Today" is fixed at 2026-10-28,
-- the reference date used to generate the data.
-- Run:  psql -d waynes_clinic -f 02_queries.sql
-- =====================================================================
\set ON_ERROR_STOP 1
\pset footer on
SET search_path TO clinic, public;
SET timezone TO 'America/New_York';
\set today '2026-10-28'

-- Parameters: a frequent attender (90th percentile of visits), a General Medicine doctor and a clinic day
WITH visits AS (SELECT patient_id, count(*) AS n FROM appointment GROUP BY patient_id)
SELECT v.patient_id AS pid, p.mrn AS mrn
FROM visits v JOIN patient p USING (patient_id)
WHERE v.n = (SELECT percentile_disc(0.9) WITHIN GROUP (ORDER BY n) FROM visits)
ORDER BY v.patient_id LIMIT 1 \gset
SELECT doctor_id AS doc FROM doctor WHERE department_id = 1 AND doctor_id > 12 ORDER BY doctor_id LIMIT 1 \gset
\set day '2026-10-20'
\echo 'Parameters: patient_id =' :pid ' mrn =' :mrn ' doctor_id =' :doc ' day =' :day

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q1  RECEPTION - Which slots are still free for this doctor on this day? (booking)'
-- Generates the day's slot grid from the doctor's sessions and removes
-- any slot overlapping a live appointment (anti-join on range overlap).
WITH doc AS (
    SELECT doctor_id, make_interval(mins => consultation_minutes) AS len
    FROM doctor WHERE doctor_id = :doc),
grid AS (
    SELECT gs AS slot_start, gs + doc.len AS slot_end
    FROM doctor_availability av JOIN doc USING (doctor_id),
         generate_series((:'day'::date + av.start_time) AT TIME ZONE 'America/New_York',
                         (:'day'::date + av.end_time)   AT TIME ZONE 'America/New_York' - doc.len,
                         doc.len) gs
    WHERE av.iso_weekday = EXTRACT(ISODOW FROM :'day'::date))
SELECT to_char(g.slot_start, 'HH24:MI') AS free_slot
FROM grid g
WHERE NOT EXISTS (
    SELECT 1 FROM appointment a
    WHERE a.doctor_id = :doc AND a.status <> 'Cancelled'
      AND tstzrange(a.scheduled_start, a.scheduled_end) && tstzrange(g.slot_start, g.slot_end))
ORDER BY g.slot_start;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q2  RECEPTION - Find a patient by surname (prefix search)'
SELECT mrn, first_name, last_name, date_of_birth, phone
FROM patient
WHERE last_name LIKE 'Okonk%' AND date_of_birth BETWEEN '1980-01-01' AND '1984-12-31'
ORDER BY last_name, first_name
LIMIT 10;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q3  DOCTOR - Full history of one patient, newest first (core operation)'
SELECT to_char(event_time, 'YYYY-MM-DD HH24:MI') AS event_time, event_type, appointment_id, left(detail, 70) AS detail
FROM v_patient_history
WHERE patient_id = :pid
  AND event_time <= (:'today')::timestamptz
ORDER BY event_time DESC
LIMIT 15;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q4  DOCTOR - My appointment list for one day'
SELECT to_char(a.scheduled_start, 'HH24:MI') AS start_time, p.mrn,
       p.first_name || ' ' || p.last_name AS patient, a.status, a.reason
FROM appointment a JOIN patient p USING (patient_id)
WHERE a.doctor_id = :doc
  AND a.scheduled_start >= (:'day')::timestamptz
  AND a.scheduled_start <  (:'day')::timestamptz + interval '1 day'
ORDER BY a.scheduled_start;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q5  LABORATORY - Critical results filed in the last 7 days (call-back worklist)'
SELECT to_char(r.resulted_at, 'MM-DD HH24:MI') AS resulted, p.mrn,
       p.first_name || ' ' || p.last_name AS patient,
       t.test_name, trim_scale(r.result_value) AS value, t.unit,
       'Dr ' || d.last_name AS requested_by
FROM lab_result r
JOIN lab_order  lo ON lo.lab_order_id  = r.lab_order_id
JOIN appointment a ON a.appointment_id = lo.appointment_id
JOIN patient    p  ON p.patient_id     = a.patient_id
JOIN doctor     d  ON d.doctor_id      = a.doctor_id
JOIN lab_test   t  ON t.test_code      = r.test_code
WHERE r.abnormal_flag = 'C'
  AND r.resulted_at >= (:'today')::timestamptz - interval '7 days'
ORDER BY r.resulted_at DESC
LIMIT 15;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q6  MANAGEMENT - Top 3 primary diagnoses per department in 2026 (window function)'
WITH counts AS (
    SELECT dpt.name AS department, dx.icd10_code, ic.description, count(*) AS cases
    FROM diagnosis dx
    JOIN appointment a  ON a.appointment_id = dx.appointment_id
    JOIN doctor     d   ON d.doctor_id      = a.doctor_id
    JOIN department dpt ON dpt.department_id = d.department_id
    JOIN icd10_code ic  ON ic.icd10_code    = dx.icd10_code
    WHERE dx.diagnosis_type = 'Primary'
      AND a.scheduled_start >= '2026-01-01' AND a.scheduled_start < '2027-01-01'
    GROUP BY dpt.name, dx.icd10_code, ic.description),
ranked AS (
    SELECT *, rank() OVER (PARTITION BY department ORDER BY cases DESC) AS rnk,
           round(100.0 * cases / sum(cases) OVER (PARTITION BY department), 1) AS pct_of_dept
    FROM counts)
SELECT department, rnk, icd10_code, left(description, 40) AS description, cases, pct_of_dept
FROM ranked WHERE rnk <= 3
ORDER BY department, rnk;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q7  OPERATIONS - Doctors with the highest no-show rates in 2026 (FILTER + HAVING)'
SELECT d.staff_number, 'Dr ' || d.first_name || ' ' || d.last_name AS doctor, dpt.name AS department,
       count(*) FILTER (WHERE a.status IN ('Completed', 'NoShow'))              AS due_to_attend,
       count(*) FILTER (WHERE a.status = 'NoShow')                               AS no_shows,
       round(100.0 * count(*) FILTER (WHERE a.status = 'NoShow')
             / nullif(count(*) FILTER (WHERE a.status IN ('Completed','NoShow')), 0), 2) AS no_show_pct
FROM appointment a
JOIN doctor d     ON d.doctor_id = a.doctor_id
JOIN department dpt ON dpt.department_id = d.department_id
WHERE a.scheduled_start >= '2026-01-01' AND a.scheduled_start < (:'today')::timestamptz
GROUP BY d.doctor_id, d.staff_number, d.first_name, d.last_name, dpt.name
HAVING count(*) FILTER (WHERE a.status IN ('Completed','NoShow')) >= 500
ORDER BY no_show_pct DESC
LIMIT 10;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q8  PATIENT SAFETY - Patients prescribed warfarin with NO INR test in the last 90 days (anti-join)'
SELECT p.mrn, p.first_name || ' ' || p.last_name AS patient,
       max(pr.issued_at)::date AS last_warfarin_rx
FROM prescription pr
JOIN prescription_item pi ON pi.prescription_id = pr.prescription_id
JOIN medication m         ON m.medication_id    = pi.medication_id AND m.generic_name = 'Warfarin'
JOIN appointment a        ON a.appointment_id   = pr.appointment_id
JOIN patient p            ON p.patient_id       = a.patient_id
WHERE pr.issued_at >= (:'today')::timestamptz - interval '180 days'
  AND NOT EXISTS (
        SELECT 1
        FROM lab_result r
        JOIN lab_order lo   ON lo.lab_order_id = r.lab_order_id
        JOIN appointment a2 ON a2.appointment_id = lo.appointment_id
        WHERE a2.patient_id = a.patient_id
          AND r.test_code = 'INR'
          AND r.resulted_at >= (:'today')::timestamptz - interval '90 days')
GROUP BY p.mrn, p.first_name, p.last_name
ORDER BY last_warfarin_rx DESC
LIMIT 10;

\echo 'Q8b How many such patients in total?'
SELECT count(DISTINCT a.patient_id) AS patients_needing_inr
FROM prescription pr
JOIN prescription_item pi ON pi.prescription_id = pr.prescription_id
JOIN medication m         ON m.medication_id    = pi.medication_id AND m.generic_name = 'Warfarin'
JOIN appointment a        ON a.appointment_id   = pr.appointment_id
WHERE pr.issued_at >= (:'today')::timestamptz - interval '180 days'
  AND NOT EXISTS (
        SELECT 1 FROM lab_result r
        JOIN lab_order lo   ON lo.lab_order_id = r.lab_order_id
        JOIN appointment a2 ON a2.appointment_id = lo.appointment_id
        WHERE a2.patient_id = a.patient_id AND r.test_code = 'INR'
          AND r.resulted_at >= (:'today')::timestamptz - interval '90 days');

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q9  RECALL - Diabetic patients whose most recent visit was over 6 months ago (DISTINCT ON)'
WITH diabetics AS (
    SELECT DISTINCT a.patient_id
    FROM diagnosis dx JOIN appointment a USING (appointment_id)
    WHERE dx.icd10_code = 'E11.9'),
last_visit AS (
    SELECT DISTINCT ON (a.patient_id) a.patient_id, a.scheduled_start, a.doctor_id
    FROM appointment a
    WHERE a.patient_id IN (SELECT patient_id FROM diabetics) AND a.status = 'Completed'
    ORDER BY a.patient_id, a.scheduled_start DESC)
SELECT p.mrn, p.first_name || ' ' || p.last_name AS patient, lv.scheduled_start::date AS last_visit,
       (:'today'::date - lv.scheduled_start::date) AS days_since
FROM last_visit lv JOIN patient p USING (patient_id)
WHERE lv.scheduled_start < (:'today')::timestamptz - interval '6 months'
ORDER BY days_since DESC
LIMIT 10;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q10 MANAGEMENT - Monthly completed visits by department, 2026 (dashboard source)'
SELECT to_char(date_trunc('month', a.scheduled_start), 'YYYY-MM') AS month,
       count(*) FILTER (WHERE dpt.name = 'General Medicine')   AS gen_med,
       count(*) FILTER (WHERE dpt.name = 'Cardiology')         AS cardio,
       count(*) FILTER (WHERE dpt.name = 'Pediatrics')         AS peds,
       count(*) FILTER (WHERE dpt.name = 'Emergency Medicine') AS emergency,
       count(*)                                                AS all_departments
FROM appointment a
JOIN doctor d       ON d.doctor_id = a.doctor_id
JOIN department dpt ON dpt.department_id = d.department_id
WHERE a.status = 'Completed' AND a.scheduled_start >= '2026-01-01'
GROUP BY 1 ORDER BY 1;

-- ---------------------------------------------------------------------
\echo ''
\echo 'Q11 PHARMACY - 30-day demand vs stock on hand (reorder report)'
SELECT m.generic_name, m.strength, m.stock_on_hand,
       sum(pi.quantity) AS qty_prescribed_30d,
       round(m.stock_on_hand / nullif(sum(pi.quantity) / 30.0, 0), 1) AS days_of_cover,
       CASE WHEN m.stock_on_hand < sum(pi.quantity) THEN 'REORDER NOW'
            WHEN m.stock_on_hand < 2 * sum(pi.quantity) THEN 'Reorder soon' ELSE 'OK' END AS action
FROM prescription pr
JOIN prescription_item pi ON pi.prescription_id = pr.prescription_id
JOIN medication m USING (medication_id)
WHERE pr.issued_at >= (:'today')::timestamptz - interval '30 days'
GROUP BY m.medication_id, m.generic_name, m.strength, m.stock_on_hand
ORDER BY days_of_cover
LIMIT 12;
