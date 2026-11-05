-- =====================================================================
-- Wayne's Clinic Baltimore — Portfolio 4 · Wide-column model (Apache Cassandra 5 / CQL)
--
-- STATUS: design artefact. No Cassandra binary could be downloaded in the
-- environment used to build this portfolio, so these statements were not
-- executed there. They are standard CQL for Cassandra 4.1+/5.0 (or Astra DB)
-- and can be run with:  docker run -d --name cass -p 9042:9042 cassandra:5
--                       docker exec -i cass cqlsh < 01_schema.cql
--
-- Cassandra is modelled QUERY-FIRST: one table per access pattern, data
-- duplicated across tables on write, no joins. The partition key decides
-- which node stores the row; clustering columns sort rows inside it.
-- =====================================================================

CREATE KEYSPACE IF NOT EXISTS waynes_clinic
  WITH replication = {'class': 'NetworkTopologyStrategy', 'us_east': 3}   -- 3 replicas in the Baltimore-region DC
  AND durable_writes = true;

USE waynes_clinic;

-- Q-A  "Show Dr X's list for day D"                      (reception, every few seconds)
-- Partition = one doctor-day (~14-40 rows): small, bounded, read in one disk seek.
CREATE TABLE IF NOT EXISTS appointments_by_doctor_day (
    doctor_id     int,
    clinic_date   date,
    start_time    time,
    appointment_id bigint,
    patient_id    int,
    patient_mrn   text,
    patient_name  text,            -- denormalised: no join available
    status        text,
    reason        text,
    PRIMARY KEY ((doctor_id, clinic_date), start_time)
) WITH CLUSTERING ORDER BY (start_time ASC)
  AND comment = 'Doctor day list; one partition per doctor per day';

-- Q-B  "Book slot S if it is still free"                 (booking)
-- Lightweight transaction (Paxos) gives compare-and-set on a single partition.
CREATE TABLE IF NOT EXISTS slot_claims (
    doctor_id   int,
    clinic_date date,
    start_time  time,
    patient_id  int,
    claimed_at  timestamp,
    PRIMARY KEY ((doctor_id, clinic_date), start_time)
);

-- Q-C  "Patient history, newest first"                   (doctor, core operation)
-- Wide row: every clinical event of a patient in one partition, pre-sorted.
CREATE TABLE IF NOT EXISTS patient_timeline (
    patient_id   int,
    event_time   timestamp,
    event_id     timeuuid,
    event_type   text,             -- Appointment | Diagnosis | Prescription | Lab result
    appointment_id bigint,
    clinician    text,
    code         text,             -- ICD-10, medication or test code
    detail       text,
    flag         text,
    PRIMARY KEY ((patient_id), event_time, event_id)
) WITH CLUSTERING ORDER BY (event_time DESC, event_id DESC);

-- Q-D  "Critical results filed today"                    (lab call-back worklist)
CREATE TABLE IF NOT EXISTS critical_results_by_day (
    result_date  date,
    resulted_at  timestamp,
    result_id    bigint,
    patient_mrn  text,
    test_code    text,
    value        decimal,
    doctor_name  text,
    acknowledged boolean,
    PRIMARY KEY ((result_date), resulted_at, result_id)
) WITH CLUSTERING ORDER BY (resulted_at DESC, result_id ASC);

-- Q-E  Bedside monitor telemetry - Cassandra's natural workload.
-- A future inpatient ward streams heart rate / SpO2 every 5 s per patient:
-- ~17,000 writes per patient per day. Partition per patient per day keeps
-- partitions bounded; TimeWindowCompaction + TTL drop raw data after 30 days.
CREATE TABLE IF NOT EXISTS vitals_by_patient_day (
    patient_id   int,
    day          date,
    recorded_at  timestamp,
    heart_rate   smallint,
    spo2         smallint,
    resp_rate    smallint,
    systolic     smallint,
    diastolic    smallint,
    device_id    text,
    PRIMARY KEY ((patient_id, day), recorded_at)
) WITH CLUSTERING ORDER BY (recorded_at DESC)
  AND compaction = {'class': 'TimeWindowCompactionStrategy', 'compaction_window_unit': 'DAYS', 'compaction_window_size': 1}
  AND default_time_to_live = 2592000;
