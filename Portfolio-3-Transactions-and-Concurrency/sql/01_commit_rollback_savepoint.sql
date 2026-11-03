-- =====================================================================
-- Wayne's Clinic Baltimore — Advanced Database Systems Portfolio
-- Portfolio 3 · Script 01 · Single-session transactions:
--     T1 COMMIT · T2 explicit ROLLBACK · T3 automatic rollback on error
--     T4 SAVEPOINT / partial rollback · T5 sequences are not transactional
-- Run:  psql -d waynes_clinic -f 01_commit_rollback_savepoint.sql
-- =====================================================================
\set ON_ERROR_STOP 0
\set SHOW_CONTEXT never
\pset footer off
SET search_path TO clinic, public;
SET timezone TO 'America/New_York';
SELECT (date_trunc('week', now() AT TIME ZONE 'America/New_York')::date + 8)::text AS next_tuesday \gset
-- First free slots next Tuesday (works on the small sample and on the bulk-loaded data)
SELECT slot_start::text AS slot_dr2  FROM available_slots(2,  :'next_tuesday'::date) LIMIT 1 \gset
SELECT slot_start::text AS slot_dr11 FROM available_slots(11, :'next_tuesday'::date) ORDER BY slot_start DESC LIMIT 1 \gset

\echo '================================================================'
\echo 'T1  COMMIT - register a walk-in patient AND book her first visit as one unit'
\echo '================================================================'
BEGIN;
SELECT pg_current_xact_id() AS txid;
INSERT INTO patient (mrn, first_name, last_name, date_of_birth, sex, blood_group, phone, zip_code)
VALUES ('WCB0099001', 'Renee', 'Calloway', '1979-06-14', 'F', 'A+', '410-555-0190', '21218')
RETURNING patient_id AS new_pid \gset
SELECT book_appointment(:new_pid, 2, :'slot_dr2'::timestamptz, 'New patient check-up') AS new_appt \gset
\echo 'Inside the transaction both rows exist:'
SELECT p.mrn, p.first_name || ' ' || p.last_name AS patient, a.appointment_id, a.status,
       to_char(a.scheduled_start, 'Dy DD Mon HH24:MI') AS slot
FROM patient p JOIN appointment a USING (patient_id) WHERE p.patient_id = :new_pid;
COMMIT;
\echo 'After COMMIT, a brand-new connection (\\c) can see them - the change is durable and visible:'
\c
SET search_path TO clinic, public;
SET timezone TO 'America/New_York';
SELECT p.mrn, p.xmin AS created_by_txid, a.appointment_id, a.status
FROM patient p JOIN appointment a USING (patient_id) WHERE p.mrn = 'WCB0099001';

\echo ''
\echo '================================================================'
\echo 'T2  EXPLICIT ROLLBACK - a pharmacist keys the wrong quantity and cancels'
\echo '================================================================'
SELECT generic_name, stock_on_hand AS stock_before FROM medication WHERE generic_name = 'Insulin Glargine';
BEGIN;
UPDATE medication SET stock_on_hand = stock_on_hand - 100 WHERE generic_name = 'Insulin Glargine';
INSERT INTO stock_movement (medication_id, quantity, reason)
SELECT medication_id, -100, 'Ward transfer (typo: meant 10)' FROM medication WHERE generic_name = 'Insulin Glargine';
\echo 'Uncommitted state seen by this session:'
SELECT m.stock_on_hand AS stock_inside_txn, (SELECT count(*) FROM stock_movement) AS movements_inside_txn
FROM medication m WHERE generic_name = 'Insulin Glargine';
ROLLBACK;
\echo 'After ROLLBACK both the stock change and the audit row are gone:'
SELECT m.stock_on_hand AS stock_after_rollback, (SELECT count(*) FROM stock_movement) AS movements_after_rollback
FROM medication m WHERE generic_name = 'Insulin Glargine';

\echo ''
\echo '================================================================'
\echo 'T3  ATOMICITY ON ERROR - diagnosis + prescription where one line breaks a rule'
\echo '    Sunday 25 Oct: James Holloway (allergic to penicillin) walks into the Emergency'
\echo '    Department with a sore throat and is checked in. The doctor stores a diagnosis'
\echo '    then prescribes Penicillin V. The allergy trigger raises WC001.'
\echo '================================================================'
-- the walk-in encounter itself (committed on its own, before the clinical transaction)
INSERT INTO appointment (patient_id, doctor_id, scheduled_start, scheduled_end, status, reason, booked_at)
VALUES (1, 9, '2026-10-25 10:00', '2026-10-25 10:15', 'CheckedIn', 'Walk-in: sore throat and fever', '2026-10-25 09:52')
RETURNING appointment_id AS ed_visit \gset
BEGIN;
SELECT record_diagnosis(:ed_visit, 'J02.9', 'Primary', 'Sore throat, fever 38.4C') AS diagnosis_id;
INSERT INTO prescription (appointment_id, notes) VALUES (:ed_visit, 'Antibiotic course') RETURNING prescription_id AS rx \gset
INSERT INTO prescription_item (prescription_id, line_no, medication_id, dosage, frequency, duration_days, quantity)
VALUES (:rx, 1, (SELECT medication_id FROM medication WHERE generic_name = 'Penicillin V Potassium'),
        '500 mg', 'Four times daily', 10, 40);
\echo 'The transaction is now in the ABORTED state - every further command is refused:'
SELECT 'this will not run';
COMMIT;
\echo '(psql printed ROLLBACK: COMMIT of a failed transaction is a rollback.)'
\echo 'Nothing from the transaction survived - not even the diagnosis or the status change:'
SELECT a.appointment_id, a.status,
       (SELECT count(*) FROM diagnosis    WHERE appointment_id = :ed_visit) AS diagnoses,
       (SELECT count(*) FROM prescription WHERE appointment_id = :ed_visit) AS prescriptions
FROM appointment a WHERE appointment_id = :ed_visit;

\echo ''
\echo '================================================================'
\echo 'T4  SAVEPOINT - keep the diagnosis, undo only the unsafe prescription line'
\echo '================================================================'
BEGIN;
SELECT record_diagnosis(:ed_visit, 'J02.9', 'Primary', 'Sore throat, fever 38.4C') AS diagnosis_id;
INSERT INTO prescription (appointment_id, notes) VALUES (:ed_visit, 'Antibiotic course') RETURNING prescription_id AS rx \gset
SAVEPOINT before_line_1;
INSERT INTO prescription_item (prescription_id, line_no, medication_id, dosage, frequency, duration_days, quantity)
VALUES (:rx, 1, (SELECT medication_id FROM medication WHERE generic_name = 'Penicillin V Potassium'),
        '500 mg', 'Four times daily', 10, 40);
ROLLBACK TO SAVEPOINT before_line_1;
\echo 'Rolled back to the savepoint - the transaction is healthy again. Prescribe the alternative:'
INSERT INTO prescription_item (prescription_id, line_no, medication_id, dosage, frequency, duration_days, quantity)
VALUES (:rx, 1, (SELECT medication_id FROM medication WHERE generic_name = 'Azithromycin'),
        '500 mg day 1, then 250 mg', 'Once daily', 5, 6);
RELEASE SAVEPOINT before_line_1;
COMMIT;
\echo 'Committed: the diagnosis and the azithromycin line are stored; penicillin is not:'
SELECT a.status, dx.icd10_code, m.generic_name AS prescribed
FROM appointment a
JOIN diagnosis dx          ON dx.appointment_id = a.appointment_id
JOIN prescription pr       ON pr.appointment_id = a.appointment_id
JOIN prescription_item pi  ON pi.prescription_id = pr.prescription_id
JOIN medication m          ON m.medication_id = pi.medication_id
WHERE a.appointment_id = :ed_visit;

\echo ''
\echo '================================================================'
\echo 'T5  WHAT IS *NOT* ROLLED BACK - sequence values'
\echo '================================================================'
SELECT last_value AS appointment_seq_before FROM appointment_appointment_id_seq;
BEGIN;
INSERT INTO appointment (patient_id, doctor_id, scheduled_start, scheduled_end)
VALUES (:new_pid, 11, :'slot_dr11'::timestamptz, :'slot_dr11'::timestamptz + interval '20 minutes')
RETURNING appointment_id AS id_used_then_discarded;
ROLLBACK;
SELECT last_value AS appointment_seq_after FROM appointment_appointment_id_seq;
\echo 'The sequence advanced even though the row was rolled back: identity values can have'
\echo 'gaps. Ids must never be used as a "count" of appointments or as an invoice number.'
