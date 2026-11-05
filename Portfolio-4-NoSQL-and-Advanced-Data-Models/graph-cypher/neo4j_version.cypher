// =====================================================================
// Wayne's Clinic Baltimore — Portfolio 4 · The same graph for Neo4j 5 / AuraDB
//
// STATUS: portability script. The graph in this portfolio was built and
// queried on Kùzu (see build_and_query_graph.py, outputs in ../evidence).
// Neo4j is schema-optional, so node/relationship tables are not declared;
// uniqueness constraints play the role of primary keys. Copy the CSV files
// produced in ./data into Neo4j's import directory, then run this file in
// cypher-shell or Neo4j Browser. Column order matches build_and_query_graph.py.
// =====================================================================

CREATE CONSTRAINT patient_id IF NOT EXISTS FOR (p:Patient)    REQUIRE p.id IS UNIQUE;
CREATE CONSTRAINT doctor_id  IF NOT EXISTS FOR (d:Doctor)     REQUIRE d.id IS UNIQUE;
CREATE CONSTRAINT dept_name  IF NOT EXISTS FOR (d:Department) REQUIRE d.name IS UNIQUE;
CREATE CONSTRAINT icd_code   IF NOT EXISTS FOR (c:Condition)  REQUIRE c.code IS UNIQUE;
CREATE CONSTRAINT med_id     IF NOT EXISTS FOR (m:Medication) REQUIRE m.id IS UNIQUE;

LOAD CSV FROM 'file:///patients.csv' AS r
MERGE (:Patient {id: toInteger(r[0]), mrn: r[1], name: r[2], birth_year: toInteger(r[3]), sex: r[4]});
LOAD CSV FROM 'file:///doctors.csv' AS r
MERGE (:Doctor {id: toInteger(r[0]), name: r[1], specialty: r[2]});
LOAD CSV FROM 'file:///departments.csv' AS r MERGE (:Department {name: r[0]});
LOAD CSV FROM 'file:///conditions.csv' AS r MERGE (:Condition {code: r[0], description: r[1]});
LOAD CSV FROM 'file:///medications.csv' AS r MERGE (:Medication {id: toInteger(r[0]), name: r[1]});

LOAD CSV FROM 'file:///works_in.csv' AS r
MATCH (d:Doctor {id: toInteger(r[0])}), (x:Department {name: r[1]}) MERGE (d)-[:WORKS_IN]->(x);

CALL {
  LOAD CSV FROM 'file:///visited.csv' AS r
  MATCH (p:Patient {id: toInteger(r[0])}), (d:Doctor {id: toInteger(r[1])})
  CREATE (p)-[:VISITED {appointment_id: toInteger(r[2]), day: date(r[3]), start_time: localdatetime(replace(r[4], ' ', 'T'))}]->(d)
} IN TRANSACTIONS OF 10000 ROWS;

CALL {
  LOAD CSV FROM 'file:///diagnosed.csv' AS r
  MATCH (p:Patient {id: toInteger(r[0])}), (c:Condition {code: r[1]})
  CREATE (p)-[:DIAGNOSED_WITH {day: date(r[2]), dx_type: r[3], appointment_id: toInteger(r[4])}]->(c)
} IN TRANSACTIONS OF 10000 ROWS;

CALL {
  LOAD CSV FROM 'file:///prescribed.csv' AS r
  MATCH (p:Patient {id: toInteger(r[0])}), (m:Medication {id: toInteger(r[1])})
  CREATE (p)-[:PRESCRIBED {day: date(r[2]), appointment_id: toInteger(r[3])}]->(m)
} IN TRANSACTIONS OF 10000 ROWS;

LOAD CSV FROM 'file:///allergic.csv' AS r
MATCH (p:Patient {id: toInteger(r[0])}), (m:Medication {id: toInteger(r[1])})
CREATE (p)-[:ALLERGIC_TO {severity: r[2]}]->(m);

LOAD CSV FROM 'file:///interacts.csv' AS r
MATCH (a:Medication {id: toInteger(r[0])}), (b:Medication {id: toInteger(r[1])})
CREATE (a)-[:INTERACTS_WITH {severity: r[2], effect: r[3]}]->(b);

// ---- G1 major interactions
MATCH (p:Patient)-[:PRESCRIBED]->(m1:Medication)-[i:INTERACTS_WITH {severity: 'Major'}]->(m2:Medication)<-[:PRESCRIBED]-(p)
RETURN m1.name, m2.name, i.effect, count(DISTINCT p) AS patients ORDER BY patients DESC;

// ---- G2 contact tracing
MATCH (c:Patient)-[d:DIAGNOSED_WITH]->(:Condition {code: 'U07.1'}),
      (c)-[v1:VISITED]->(doc:Doctor)<-[v2:VISITED]-(o:Patient)
WHERE v1.appointment_id = d.appointment_id AND v2.day = v1.day AND o <> c
RETURN count(DISTINCT c) AS covid_cases, count(DISTINCT o) AS exposed_patients, count(*) AS exposure_events;

// ---- G5 shortest path (Neo4j syntax differs from Kùzu's  [* SHORTEST 1..8])
MATCH (a:Patient {mrn: 'WCB0000003'}), (b:Patient {mrn: 'WCB0000016'})
MATCH path = shortestPath((a)-[:VISITED*1..8]-(b))
RETURN length(path) AS hops, [n IN nodes(path) | coalesce(n.mrn, n.name)] AS chain;
