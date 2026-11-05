// =====================================================================
// Wayne's Clinic Baltimore — Portfolio 4 · Property-graph model (Cypher)
// Executed on Kùzu 0.11 (embedded graph DB). Kùzu requires declared node
// and relationship tables; for Neo4j drop these statements and use the
// MERGE-based loader in neo4j_version.cypher.
// =====================================================================

// ---- Nodes
CREATE NODE TABLE Patient    (id INT64, mrn STRING, name STRING, birth_year INT64, sex STRING, PRIMARY KEY (id));
CREATE NODE TABLE Doctor     (id INT64, name STRING, specialty STRING, PRIMARY KEY (id));
CREATE NODE TABLE Department (name STRING, PRIMARY KEY (name));
CREATE NODE TABLE Condition  (code STRING, description STRING, PRIMARY KEY (code));
CREATE NODE TABLE Medication (id INT64, name STRING, PRIMARY KEY (id));

// ---- Relationships (edges carry the facts of the encounter)
CREATE REL TABLE WORKS_IN       (FROM Doctor  TO Department);
CREATE REL TABLE VISITED        (FROM Patient TO Doctor,     appointment_id INT64, day DATE, start_time TIMESTAMP);
CREATE REL TABLE DIAGNOSED_WITH (FROM Patient TO Condition,  day DATE, dx_type STRING, appointment_id INT64);
CREATE REL TABLE PRESCRIBED     (FROM Patient TO Medication, day DATE, appointment_id INT64);
CREATE REL TABLE ALLERGIC_TO    (FROM Patient TO Medication, severity STRING);
CREATE REL TABLE INTERACTS_WITH (FROM Medication TO Medication, severity STRING, effect STRING);
