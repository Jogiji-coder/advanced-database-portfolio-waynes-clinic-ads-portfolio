-- Runs once when the primary container initialises (docker-entrypoint-initdb.d)
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD 'replicator-demo-only';
SELECT pg_create_physical_replication_slot('slot_sync');
SELECT pg_create_physical_replication_slot('slot_async');
