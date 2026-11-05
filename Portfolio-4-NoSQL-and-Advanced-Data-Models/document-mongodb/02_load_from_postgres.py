"""
Portfolio 4 - migrate the relational clinic data into the MongoDB document model.

PostgreSQL itself assembles each document with json_build_object / json_agg,
so the mapping from tables to documents is visible in one place (the SQL
below). Documents are then bulk-inserted with pymongo.

    mongosh .../waynes_clinic 01_create_collections.js
    python 02_load_from_postgres.py
"""
import json
import os
import time
from datetime import datetime

import psycopg
from pymongo import MongoClient

PG = os.environ.get("WC_DSN", "host=localhost dbname=waynes_clinic user=postgres password=postgres")
MONGO = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27017/?replicaSet=rs0")
BATCH = 5000

DOCTORS = """
SELECT json_build_object(
  '_id', d.doctor_id, 'staffNumber', d.staff_number,
  'name', json_build_object('first', d.first_name, 'last', d.last_name),
  'specialty', s.name,
  'department', json_build_object('id', dp.department_id, 'name', dp.name, 'floor', dp.floor_number),
  'licenseNumber', d.license_number,
  'contact', json_build_object('email', d.email, 'phone', d.phone),
  'consultationMinutes', d.consultation_minutes, 'active', d.is_active,
  'availability', (SELECT coalesce(json_agg(json_build_object('weekday', iso_weekday,
                       'start', to_char(start_time, 'HH24:MI'), 'end', to_char(end_time, 'HH24:MI'))
                       ORDER BY iso_weekday, start_time), '[]')
                   FROM clinic.doctor_availability av WHERE av.doctor_id = d.doctor_id))
FROM clinic.doctor d
JOIN clinic.specialty s   ON s.specialty_id = d.specialty_id
JOIN clinic.department dp ON dp.department_id = d.department_id
ORDER BY d.doctor_id"""

PATIENTS = """
SELECT json_build_object(
  '_id', p.patient_id, 'mrn', p.mrn,
  'name', json_build_object('first', p.first_name, 'last', p.last_name),
  'dateOfBirth', p.date_of_birth, 'sex', p.sex, 'bloodGroup', p.blood_group,
  'contact', json_strip_nulls(json_build_object('phone', p.phone, 'email', p.email)),
  'address', json_build_object('street', p.street_address, 'zip', p.zip_code, 'city', pa.city, 'state', pa.state_code),
  'emergencyContact', CASE WHEN p.emergency_contact_name IS NOT NULL THEN
        json_build_object('name', p.emergency_contact_name, 'phone', p.emergency_contact_phone) END,
  'allergies', (SELECT coalesce(json_agg(json_build_object('medicationId', m.medication_id, 'medication', m.generic_name,
                    'reaction', al.reaction, 'severity', al.severity, 'recordedAt', al.recorded_at)), '[]')
                FROM clinic.patient_allergy al JOIN clinic.medication m USING (medication_id)
                WHERE al.patient_id = p.patient_id),
  'registeredAt', p.registered_at)
FROM clinic.patient p LEFT JOIN clinic.postal_area pa USING (zip_code)
ORDER BY p.patient_id"""

ENCOUNTERS = """
SELECT json_build_object(
  '_id', a.appointment_id, 'patientId', a.patient_id, 'mrn', p.mrn,
  'patientName', p.first_name || ' ' || p.last_name,
  'doctor', json_build_object('id', d.doctor_id, 'name', 'Dr ' || d.first_name || ' ' || d.last_name,
                              'specialty', s.name, 'department', dp.name),
  'start', a.scheduled_start, 'end', a.scheduled_end, 'status', a.status, 'reason', a.reason,
  'bookedAt', a.booked_at,
  'diagnoses', (SELECT coalesce(json_agg(json_build_object('code', dx.icd10_code, 'description', ic.description,
                    'type', dx.diagnosis_type, 'notes', dx.clinical_notes, 'diagnosedAt', dx.diagnosed_at)
                    ORDER BY dx.diagnosis_type), '[]')
                FROM clinic.diagnosis dx JOIN clinic.icd10_code ic USING (icd10_code)
                WHERE dx.appointment_id = a.appointment_id),
  'prescriptions', (SELECT coalesce(json_agg(json_build_object('prescriptionId', pr.prescription_id,
                    'issuedAt', pr.issued_at, 'status', pr.status,
                    'items', (SELECT json_agg(json_build_object('line', pi.line_no, 'medicationId', m.medication_id,
                                 'medication', m.generic_name, 'strength', m.strength, 'dosage', pi.dosage,
                                 'frequency', pi.frequency, 'days', pi.duration_days, 'quantity', pi.quantity)
                                 ORDER BY pi.line_no)
                              FROM clinic.prescription_item pi JOIN clinic.medication m USING (medication_id)
                              WHERE pi.prescription_id = pr.prescription_id))), '[]')
                    FROM clinic.prescription pr WHERE pr.appointment_id = a.appointment_id),
  'labOrders', (SELECT coalesce(json_agg(json_build_object('labOrderId', lo.lab_order_id, 'orderedAt', lo.ordered_at,
                    'priority', lo.priority, 'status', lo.status,
                    'results', (SELECT json_agg(json_build_object('test', r.test_code, 'name', t.test_name,
                                   'value', r.result_value, 'text', r.result_text, 'unit', t.unit,
                                   'flag', r.abnormal_flag, 'resultedAt', r.resulted_at) ORDER BY r.test_code)
                                FROM clinic.lab_result r JOIN clinic.lab_test t USING (test_code)
                                WHERE r.lab_order_id = lo.lab_order_id))), '[]')
                FROM clinic.lab_order lo WHERE lo.appointment_id = a.appointment_id))
FROM clinic.appointment a
JOIN clinic.patient p     ON p.patient_id = a.patient_id
JOIN clinic.doctor d      ON d.doctor_id = a.doctor_id
JOIN clinic.specialty s   ON s.specialty_id = d.specialty_id
JOIN clinic.department dp ON dp.department_id = d.department_id
ORDER BY a.appointment_id"""

DATE_KEYS = {"dateOfBirth", "registeredAt", "recordedAt", "start", "end", "bookedAt", "diagnosedAt",
             "issuedAt", "orderedAt", "resultedAt"}


def fix(obj):
    """JSON from PostgreSQL carries dates as ISO strings; store real BSON dates."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if v is None and k in ("emergencyContact", "text", "notes", "email"):
                continue
            if k in DATE_KEYS and isinstance(v, str) and len(v) >= 10:   # not "09:00" session times
                out[k] = datetime.fromisoformat(v)
            else:
                out[k] = fix(v)
        return out
    if isinstance(obj, list):
        return [fix(x) for x in obj]
    return obj


def copy(pg, coll, sql):
    t0, n, buf = time.perf_counter(), 0, []
    with pg.cursor(name=f"cur_{coll.name}") as cur:
        cur.itersize = BATCH
        cur.execute(sql)
        for (doc,) in cur:
            buf.append(fix(doc))
            if len(buf) == BATCH:
                coll.insert_many(buf, ordered=False); n += len(buf); buf = []
        if buf:
            coll.insert_many(buf, ordered=False); n += len(buf)
    print(f"  {coll.name:<11} {n:>8,} documents in {time.perf_counter() - t0:6.1f} s")
    return n


def main():
    client = MongoClient(MONGO)
    db = client.waynes_clinic
    with psycopg.connect(PG) as pg:
        pg.execute("SET timezone TO 'UTC'")
        counts = {name: copy(pg, db[name], sql) for name, sql in
                  [("doctors", DOCTORS), ("patients", PATIENTS), ("encounters", ENCOUNTERS)]}
    # Indexes needed by the booking rules (the history index is created in 03 so its effect can be shown)
    db.patients.create_index("mrn", unique=True, name="ux_mrn")
    db.encounters.create_index(
        [("doctor.id", 1), ("start", 1)], unique=True, name="ux_doctor_slot_live",
        partialFilterExpression={"status": {"$in": ["Scheduled", "CheckedIn", "Completed", "NoShow"]}})
    sample = db.encounters.find_one({"_id": 30})
    stats = db.command("dbStats")
    report = {"documents": counts, "dataSizeMB": round(stats["dataSize"] / 1e6, 1),
              "storageSizeMB": round(stats["storageSize"] / 1e6, 1),
              "avgEncounterBytes": round(db.command("collStats", "encounters")["avgObjSize"])}
    print(json.dumps(report, indent=2))
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "sample_encounter_document.json"), "w") as f:
        json.dump(sample, f, indent=2, default=str)
    with open(os.path.join(here, "sample_patient_document.json"), "w") as f:
        json.dump(db.patients.find_one({"_id": 1}), f, indent=2, default=str)
    with open(os.path.join(here, "..", "evidence", "mongo_load_report.json"), "w") as f:
        json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
