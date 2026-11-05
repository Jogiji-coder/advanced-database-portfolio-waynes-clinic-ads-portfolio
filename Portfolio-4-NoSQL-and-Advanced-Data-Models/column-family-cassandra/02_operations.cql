-- =====================================================================
-- Wayne's Clinic Baltimore — Portfolio 4 · Wide-column model: core operations in CQL
-- STATUS: design artefact (see 01_schema.cql). Run in cqlsh after 01_schema.cql.
-- =====================================================================
USE waynes_clinic;
CONSISTENCY LOCAL_QUORUM;      -- 2 of 3 replicas must answer: R + W > N  => read-your-writes

-- ---------------------------------------------------------------- 1. BOOK
-- Lightweight transaction: succeeds only if nobody holds the slot ([applied] = True/False)
INSERT INTO slot_claims (doctor_id, clinic_date, start_time, patient_id, claimed_at)
VALUES (1, '2026-11-04', '09:00', 19, toTimestamp(now()))
IF NOT EXISTS;
-- a second receptionist gets [applied] = False and the current holder back:
INSERT INTO slot_claims (doctor_id, clinic_date, start_time, patient_id, claimed_at)
VALUES (1, '2026-11-04', '09:00', 16, toTimestamp(now()))
IF NOT EXISTS;

-- only after a successful claim is the appointment written to every query table
BEGIN BATCH            -- LOGGED batch: all-or-nothing across tables (not isolated, not a SQL transaction)
  INSERT INTO appointments_by_doctor_day (doctor_id, clinic_date, start_time, appointment_id, patient_id,
                                          patient_mrn, patient_name, status, reason)
  VALUES (1, '2026-11-04', '09:00', 600001, 19, 'WCB0000019', 'Kevin Park', 'Scheduled', 'Post-COVID check');
  INSERT INTO patient_timeline (patient_id, event_time, event_id, event_type, appointment_id, clinician, detail)
  VALUES (19, '2026-11-04 14:00:00+0000', now(), 'Appointment', 600001, 'Dr Amara Okafor', 'Scheduled - Post-COVID check');
APPLY BATCH;

-- ------------------------------------------------------- 2. STORE DIAGNOSIS
BEGIN BATCH
  UPDATE appointments_by_doctor_day SET status = 'Completed'
   WHERE doctor_id = 1 AND clinic_date = '2026-11-04' AND start_time = '09:00';
  INSERT INTO patient_timeline (patient_id, event_time, event_id, event_type, appointment_id, clinician, code, detail)
  VALUES (19, '2026-11-04 14:20:00+0000', now(), 'Diagnosis', 600001, 'Dr Amara Okafor', 'U07.1',
          'COVID-19 - resolved, mild fatigue');
APPLY BATCH;

-- ------------------------------------------------------ 3. PATIENT HISTORY
-- Single-partition read, already sorted newest-first on disk
SELECT event_time, event_type, clinician, code, detail
FROM patient_timeline
WHERE patient_id = 19
LIMIT 50;

-- --------------------------------------------------- doctor's day list (Q-A)
SELECT start_time, patient_mrn, patient_name, status
FROM appointments_by_doctor_day
WHERE doctor_id = 1 AND clinic_date = '2026-11-04';

-- --------------------------------------------- what Cassandra will NOT do
-- No joins, no ad-hoc filters on non-key columns:
-- SELECT * FROM patient_timeline WHERE code = 'U07.1';
--   -> InvalidRequest: ... use ALLOW FILTERING   (a full-cluster scan - never in production)
-- Cross-row rules (no double booking of OVERLAPPING times, >= 1 doctor on call)
-- cannot be declared; they must be designed around (fixed slot grid + LWT, as above).

-- --------------------------------------------- telemetry (the natural workload)
INSERT INTO vitals_by_patient_day (patient_id, day, recorded_at, heart_rate, spo2, resp_rate, systolic, diastolic, device_id)
VALUES (3, '2026-10-28', '2026-10-28 18:00:05+0000', 112, 94, 20, 138, 86, 'WARD3-BED12');
SELECT recorded_at, heart_rate, spo2
FROM vitals_by_patient_day
WHERE patient_id = 3 AND day = '2026-10-28' AND recorded_at > '2026-10-28 17:00:00+0000';
