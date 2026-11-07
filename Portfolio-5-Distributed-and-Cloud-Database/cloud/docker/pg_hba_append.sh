#!/bin/bash
# Allows the replicator role to stream WAL from inside the compose network only.
echo "host replication replicator samenet scram-sha-256" >> "$PGDATA/pg_hba.conf"
