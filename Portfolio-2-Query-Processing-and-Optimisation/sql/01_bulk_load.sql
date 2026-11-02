-- =====================================================================
-- Wayne's Clinic Baltimore — Advanced Database Systems Portfolio
-- Portfolio 2 · Script 01 · Scale the database up to a realistic volume
--
-- Run AFTER Portfolio 1 (schema + seed). Adds ~50 000 patients, 200 doctors
-- and two years (2025-2026) of appointments with their diagnoses,
-- prescriptions and lab results — roughly 2.5 million rows in total.
-- setseed() + a serial plan make the data identical on every run.
-- =====================================================================
\set ON_ERROR_STOP 1
\timing on
SET search_path TO clinic, public;
SET timezone TO 'America/New_York';
SET max_parallel_workers_per_gather = 0;       -- deterministic random() order
SELECT setseed(0.2026);

-- Remember where the Portfolio 1 sample data ends
ANALYZE;
SELECT max(appointment_id)  AS seed_appt,
       (SELECT max(prescription_id) FROM prescription) AS seed_rx,
       (SELECT max(lab_order_id)    FROM lab_order)    AS seed_lab
FROM appointment \gset

-- --------------------------------------------------------- 188 more doctors
WITH dept_spec(department_id, specialty_id, weight) AS (VALUES
    (1, 1, 30), (1, 2, 20), (2, 3, 18), (3, 4, 22), (4, 5, 14), (5, 6, 10),
    (7, 8, 26), (8, 9, 12), (9, 10, 10), (10, 11, 16)),
expanded AS (SELECT row_number() OVER () AS rn, department_id, specialty_id
             FROM dept_spec, generate_series(1, weight)),
pick AS (SELECT g AS n, 1 + floor(random() * 178)::int AS rn FROM generate_series(13, 200) g)
INSERT INTO doctor (staff_number, first_name, last_name, specialty_id, department_id,
                    license_number, email, hire_date, consultation_minutes)
SELECT 'WCB-D' || lpad(n::text, 4, '0'),
       (ARRAY['Alex','Jordan','Taylor','Morgan','Casey','Riley','Jamie','Avery','Quinn','Rowan',
              'Elena','Omar','Mei','Arjun','Ngozi','Lucas','Sara','Ibrahim','Hana','Mateo'])[1 + n % 20],
       (ARRAY['Adams','Baker','Chen','Diaz','Evans','Foster','Garcia','Hughes','Ibekwe','Johnson',
              'Khan','Lopez','Murphy','Novak','Owens','Patel','Reyes','Silva','Turner','Walsh'])[1 + (n / 20 + n) % 20],  -- unique name pairs
       e.specialty_id, e.department_id,
       'D01' || lpad((n * 37)::text, 5, '0'),
       'doctor' || n || '@wayneclinic.example',
       DATE '2008-01-01' + (random() * 6000)::int,
       30
FROM pick JOIN expanded e USING (rn)
ORDER BY n;

INSERT INTO doctor_availability (doctor_id, iso_weekday, start_time, end_time)
SELECT d.doctor_id, wd, s.st, s.et
FROM doctor d
CROSS JOIN generate_series(1, 5) wd
CROSS JOIN (VALUES (time '09:00', time '12:30'), (time '13:30', time '17:00')) s(st, et)
WHERE d.doctor_id > 12;

-- -------------------------------------------------------- 50 000 patients
INSERT INTO patient (mrn, first_name, last_name, date_of_birth, sex, blood_group, phone,
                     street_address, zip_code, registered_at)
SELECT 'WCB' || lpad((20 + g)::text, 7, '0'),
       (ARRAY['James','Mary','Robert','Patricia','John','Jennifer','Michael','Linda','David','Elizabeth',
              'William','Barbara','Richard','Susan','Joseph','Jessica','Thomas','Sarah','Charles','Karen',
              'Christopher','Lisa','Daniel','Nancy','Matthew','Betty','Anthony','Sandra','Mark','Ashley',
              'Kwame','Aaliyah','Mohammed','Fatima','Wei','Priya','Carlos','Lucia','Chidi','Amara'])[1 + floor(random() * 40)::int],
       (ARRAY['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Rodriguez','Martinez',
              'Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore','Jackson','Martin',
              'Lee','Perez','Thompson','White','Harris','Sanchez','Clark','Ramirez','Lewis','Robinson',
              'Walker','Young','Allen','King','Wright','Scott','Torres','Nguyen','Hill','Flores',
              'Green','Adams','Nelson','Baker','Hall','Rivera','Campbell','Mitchell','Carter','Roberts',
              'Okafor','Mensah','Adeyemi','Nwosu','Diallo','Kowalski','Fitzgerald','Dubois','Rossi','Okonkwo'])[1 + floor(random() * 60)::int],
       DATE '1935-01-01' + (random() * 32500)::int,
       (ARRAY['F','M','F','M','F','M','F','M','X','U'])[1 + floor(random() * 10)::int],
       (ARRAY['O+','O+','O+','A+','A+','A+','B+','B+','AB+','O-','A-','B-','AB-'])[1 + floor(random() * 13)::int],
       (ARRAY['410','443','667'])[1 + floor(random() * 3)::int] || '-555-' || lpad(floor(random() * 10000)::int::text, 4, '0'),
       (100 + floor(random() * 9800)::int) || ' ' ||
       (ARRAY['Charles St','St Paul St','Eastern Ave','Harford Rd','York Rd','Liberty Heights Ave',
              'Edmondson Ave','Belair Rd','Greenmount Ave','Light St','Fort Ave','Pulaski Hwy'])[1 + floor(random() * 12)::int],
       (SELECT zip_code FROM postal_area ORDER BY random() + g * 0 LIMIT 1),
       TIMESTAMPTZ '2010-01-01' + random() * interval '5800 days'
FROM generate_series(1, 50000) g;

SELECT min(patient_id) AS p_lo, max(patient_id) - min(patient_id) + 1 AS p_n
FROM patient WHERE mrn > 'WCB0000020' \gset

-- A few thousand documented allergies so the allergy trigger has work to do
INSERT INTO patient_allergy (patient_id, medication_id, reaction, severity)
SELECT DISTINCT ON (p, m) p, m,
       (ARRAY['Rash','Hives','Anaphylaxis','Swelling','Nausea','Bronchospasm'])[1 + floor(random() * 6)::int],
       (ARRAY['Mild','Mild','Moderate','Severe'])[1 + floor(random() * 4)::int]
FROM (SELECT :p_lo + floor(random() * :p_n)::int AS p,
             (ARRAY[4, 12, 5, 16, 3, 2])[1 + floor(random() * 6)::int] AS m
      FROM generate_series(1, 2500)) x
ON CONFLICT DO NOTHING;

-- --------------------------------------------- two years of appointments
-- Candidate slots on every weekday clinic session of every doctor, filled
-- at ~40 %. Patient choice is mildly skewed (power 1.15) so some patients are
-- frequent attenders (about 4x the average), as in real clinics. Rows are inserted in time order,
-- so the heap is physically correlated with scheduled_start (used by BRIN).
CREATE TEMP TABLE reason_by_dept(department_id int, reasons text[]);
INSERT INTO reason_by_dept VALUES
 (1,  ARRAY['Annual physical','Cough and cold','Blood pressure check','Medication review','Fatigue','Back pain']),
 (2,  ARRAY['Chest pain','Palpitations','Hypertension review','Cholesterol review','Shortness of breath']),
 (3,  ARRAY['Well-child visit','Fever','Ear pain','Vaccination','Asthma review']),
 (4,  ARRAY['Knee pain','Ankle sprain','Back pain','Fracture follow-up','Shoulder pain']),
 (5,  ARRAY['Diabetes review','Thyroid check','Weight management']),
 (7,  ARRAY['Abdominal pain','Injury','Fever','Chest pain','Shortness of breath']),
 (8,  ARRAY['Headache','Migraine follow-up','Dizziness','Numbness']),
 (9,  ARRAY['Rash','Eczema review','Mole check','Acne']),
 (10, ARRAY['Prenatal visit','Annual exam','Pelvic pain','Contraception advice']);

INSERT INTO appointment (patient_id, doctor_id, scheduled_start, scheduled_end, status, reason, booked_at)
SELECT patient_id, doctor_id, s_start, s_start + make_interval(mins => mins), status::appointment_status,
       reason, s_start - make_interval(days => 1 + floor(random() * 30)::int)
FROM (
    SELECT :p_lo + floor(:p_n * power(random(), 1.15))::int AS patient_id,
           d.doctor_id, d.consultation_minutes AS mins,
           ((day + av.start_time) AT TIME ZONE 'America/New_York')
               + make_interval(mins => k * d.consultation_minutes) AS s_start,
           CASE WHEN day < DATE '2026-10-28' THEN
                    (ARRAY['Completed','Completed','Completed','Completed','Completed','Completed',
                           'Completed','Completed','Completed','Completed','Completed','Completed',
                           'Completed','Completed','Completed','Completed','Completed','NoShow','NoShow','Cancelled'])[1 + floor(random() * 20)::int]
                ELSE (ARRAY['Scheduled','Scheduled','Scheduled','Scheduled','Scheduled','Scheduled',
                            'Scheduled','Scheduled','Scheduled','Cancelled'])[1 + floor(random() * 10)::int]
           END AS status,
           coalesce(rb.reasons[1 + floor(random() * array_length(rb.reasons, 1))::int], 'Consultation') AS reason
    FROM generate_series(DATE '2025-01-01', DATE '2026-12-31', interval '1 day') AS day
    JOIN doctor_availability av ON av.iso_weekday = EXTRACT(ISODOW FROM day) AND av.iso_weekday <= 5
    JOIN doctor d ON d.doctor_id = av.doctor_id
    LEFT JOIN reason_by_dept rb ON rb.department_id = d.department_id
    CROSS JOIN LATERAL generate_series(0,
        (EXTRACT(EPOCH FROM av.end_time - av.start_time) / 60 / d.consultation_minutes)::int - 1) AS k
    WHERE random() < 0.40
    ORDER BY s_start, d.doctor_id
) slots
ON CONFLICT DO NOTHING;      -- skips the rare patient double-booking (exclusion constraint)

-- Refresh planner statistics after each large load: with stale statistics the
-- planner still believes the table holds ~40 rows and chooses nested loops.
ANALYZE appointment;

-- ------------------------------------------------------------ diagnoses
-- One Primary diagnosis per attended visit, plus a Secondary for ~30 %.
CREATE TEMP TABLE dx_by_dept(department_id int, codes text[]);
INSERT INTO dx_by_dept VALUES
 (1,  ARRAY['I10','E11.9','J06.9','E78.5','K21.9','F41.1','Z00.00','M54.50','E66.9','N39.0','Z23','R51.9']),
 (2,  ARRAY['I10','I25.10','I48.91','E78.5','R07.9']),
 (3,  ARRAY['J06.9','J02.9','J45.909','L20.9','Z23','B34.9']),
 (4,  ARRAY['S93.401A','M54.50']),
 (5,  ARRAY['E11.9','E03.9','E66.9','E78.5']),
 (6,  ARRAY['D64.9']),
 (7,  ARRAY['R07.9','R10.9','J18.9','U07.1','S93.401A','J06.9']),
 (8,  ARRAY['G43.909','R51.9']),
 (9,  ARRAY['L20.9']),
 (10, ARRAY['D64.9','N39.0','Z00.00']);
INSERT INTO icd10_code VALUES ('B34.9', 'Viral infection, unspecified', 'Certain infectious and parasitic diseases')
ON CONFLICT DO NOTHING;

INSERT INTO diagnosis (appointment_id, icd10_code, diagnosis_type, diagnosed_at)
SELECT a.appointment_id,
       x.codes[1 + floor(random() * array_length(x.codes, 1))::int],
       'Primary',
       a.scheduled_start + interval '20 minutes'
FROM appointment a
JOIN doctor d     ON d.doctor_id = a.doctor_id
JOIN dx_by_dept x ON x.department_id = d.department_id
WHERE a.appointment_id > :seed_appt AND a.status = 'Completed'
ORDER BY a.scheduled_start;

INSERT INTO diagnosis (appointment_id, icd10_code, diagnosis_type, diagnosed_at)
SELECT dx.appointment_id, sec.code, 'Secondary', dx.diagnosed_at
FROM diagnosis dx
-- NB: the lateral subquery must reference the outer row, otherwise PostgreSQL
-- evaluates random() once and every row receives the same value.
CROSS JOIN LATERAL (SELECT (ARRAY['E66.9','I10','E78.5','F41.1','E11.9'])[1 + floor(random() * 5 + 0 * dx.appointment_id)::int] AS code) sec
WHERE dx.appointment_id > :seed_appt AND random() < 0.30
ON CONFLICT DO NOTHING;
ANALYZE diagnosis;

-- -------------------------------------------------------- prescriptions
INSERT INTO prescription (appointment_id, issued_at, status)
SELECT a.appointment_id, a.scheduled_start + interval '25 minutes',
       CASE WHEN random() < 0.9 THEN 'Dispensed' ELSE 'Expired' END
FROM appointment a
WHERE a.appointment_id > :seed_appt AND a.status = 'Completed' AND random() < 0.60
ORDER BY a.scheduled_start;
ANALYZE prescription;

-- 1-3 distinct medications per prescription; never one the patient is allergic to
INSERT INTO prescription_item (prescription_id, line_no, medication_id, dosage, frequency, duration_days, quantity)
SELECT p.prescription_id, j + 1, med,
       '1 unit', (ARRAY['Once daily','Twice daily','Three times daily','As needed'])[1 + floor(random() * 4)::int],
       (ARRAY[5, 7, 10, 14, 30, 90])[1 + floor(random() * 6)::int],
       (ARRAY[5, 10, 14, 21, 30, 60, 90])[1 + floor(random() * 7)::int]
FROM prescription p
JOIN appointment a ON a.appointment_id = p.appointment_id
CROSS JOIN LATERAL (SELECT floor(random() * 23 + 0 * p.prescription_id)::int AS base,
                           1 + floor(random() * 3 + 0 * p.prescription_id)::int AS n) r   -- correlated: one draw per row
CROSS JOIN LATERAL generate_series(0, r.n - 1) AS j
CROSS JOIN LATERAL (SELECT ((r.base + j * 7) % 23) + 1 AS med) m
WHERE p.prescription_id > :seed_rx
  AND NOT EXISTS (SELECT 1 FROM patient_allergy pa
                  WHERE pa.patient_id = a.patient_id AND pa.medication_id = m.med);
ANALYZE prescription_item;

-- ---------------------------------------------------- lab orders/results
INSERT INTO lab_order (appointment_id, ordered_at, priority, status)
SELECT a.appointment_id, a.scheduled_start + interval '15 minutes',
       CASE WHEN d.department_id = 7 THEN 'STAT' WHEN random() < 0.1 THEN 'Urgent' ELSE 'Routine' END,
       'Completed'
FROM appointment a JOIN doctor d ON d.doctor_id = a.doctor_id
WHERE a.appointment_id > :seed_appt AND a.status = 'Completed' AND random() < 0.35
ORDER BY a.scheduled_start;
ANALYZE lab_order;

CREATE TEMP TABLE gen_range(idx int, test_code text, lo numeric, hi numeric, dp int);
INSERT INTO gen_range VALUES
 (0,'HGB',8,18.5,1),(1,'WBC',2.5,16,1),(2,'PLT',90,520,0),(3,'GLU',60,240,0),(4,'HBA1C',4.2,10.5,1),
 (5,'CHOL',120,290,0),(6,'LDL',45,200,0),(7,'HDL',25,90,0),(8,'TSH',0.1,9,2),(9,'CREAT',0.5,2.4,2),
 (10,'NA',128,148,0),(11,'K',3.0,5.8,1),(12,'ALT',8,120,0),(13,'CRP',0.2,45,1),(14,'TROP',1,60,0),(15,'INR',0.8,3.4,1);

-- Up to 4 distinct tests per order; resulted_at follows time order (BRIN-friendly)
INSERT INTO lab_result (lab_order_id, test_code, result_value, resulted_at)
SELECT lo.lab_order_id, g.test_code,
       round((g.lo + (g.hi - g.lo) * power(random(), 1.3))::numeric, g.dp),
       lo.ordered_at + interval '20 hours' + random() * interval '8 hours'
FROM lab_order lo
CROSS JOIN LATERAL (SELECT floor(random() * 16 + 0 * lo.lab_order_id)::int AS base,
                           1 + floor(random() * 4 + 0 * lo.lab_order_id)::int AS n) r      -- correlated: one draw per row
CROSS JOIN LATERAL generate_series(0, r.n - 1) AS j
JOIN gen_range g ON g.idx = (r.base + j * 5) % 16
WHERE lo.lab_order_id > :seed_lab
ORDER BY lo.ordered_at, j;

-- Pharmacy stock sized to between ~0.4 and 3 months of current demand
UPDATE medication m
SET stock_on_hand = (d.qty * (0.4 + random() * 2.6))::int
FROM (SELECT pi.medication_id, sum(pi.quantity) AS qty
      FROM prescription_item pi JOIN prescription pr USING (prescription_id)
      WHERE pr.issued_at >= TIMESTAMPTZ '2026-09-28'
      GROUP BY pi.medication_id) d
WHERE d.medication_id = m.medication_id;

-- ------------------------------------------------------------- housekeeping
RESET max_parallel_workers_per_gather;
VACUUM (ANALYZE);

\echo 'Row counts after bulk load:'
SELECT 'patient' AS table_name, count(*) FROM patient UNION ALL
SELECT 'doctor', count(*) FROM doctor UNION ALL
SELECT 'appointment', count(*) FROM appointment UNION ALL
SELECT 'diagnosis', count(*) FROM diagnosis UNION ALL
SELECT 'prescription', count(*) FROM prescription UNION ALL
SELECT 'prescription_item', count(*) FROM prescription_item UNION ALL
SELECT 'lab_order', count(*) FROM lab_order UNION ALL
SELECT 'lab_result', count(*) FROM lab_result UNION ALL
SELECT 'patient_allergy', count(*) FROM patient_allergy;

SELECT pg_size_pretty(pg_database_size('waynes_clinic')) AS database_size;
