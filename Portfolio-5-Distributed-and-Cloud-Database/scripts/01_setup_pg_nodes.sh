#!/usr/bin/env bash
# =====================================================================
# Portfolio 5 - build a local multi-node PostgreSQL 16 topology.
#
#   REPLICATION GROUP                         SHARDED GROUP
#   pg-primary      :5433  (read/write)       pg-coordinator :5440  (postgres_fdw router)
#   replica_sync    :5434  synchronous        pg-shard1      :5441
#   replica_async   :5435  asynchronous,      pg-shard2      :5442
#                          3 s apply delay (simulates a distant DR region)
#
# Every node is a separate PostgreSQL server process with its own data
# directory, WAL and port, i.e. a separate machine in miniature. Trust
# authentication on localhost is used for the demo ONLY - see
# ../cloud/ for the production security settings.
# Run as root (it switches to the postgres OS user).
# =====================================================================
set -euo pipefail
BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}
BASE=${P5_BASE:-/var/lib/postgresql/p5}
HERE="$(cd "$(dirname "$0")" && pwd)"
P1="$HERE/../../Portfolio-1-Database-Design-and-Modelling/sql"
as_pg() { su postgres -c "$*"; }

for d in primary replica_sync replica_async coordinator shard1 shard2; do
  [ -f "$BASE/$d/postmaster.pid" ] && as_pg "$BIN/pg_ctl -D $BASE/$d stop -m immediate" >/dev/null 2>&1 || true
done
rm -rf "$BASE"; mkdir -p "$BASE/logs"; chown -R postgres:postgres "$BASE"

common_conf() {   # $1 = data dir, $2 = port
  cat >> "$1/postgresql.conf" <<CONF
listen_addresses = 'localhost'
port = $2
shared_buffers = 64MB
max_connections = 50
wal_level = replica
max_wal_senders = 10
max_replication_slots = 10
hot_standby = on
wal_receiver_status_interval = 1s
wal_log_hints = on
log_line_prefix = '%m [%p] %q%a '
logging_collector = off
CONF
  echo "host all,replication all 127.0.0.1/32 trust" >> "$1/pg_hba.conf"
}

echo ">> primary :5433"
as_pg "$BIN/initdb -D $BASE/primary --data-checksums -U postgres --auth=trust -E UTF8 --locale=C.UTF-8" >/dev/null
common_conf "$BASE/primary" 5433
cat >> "$BASE/primary/postgresql.conf" <<'CONF'
synchronous_standby_names = 'FIRST 1 (replica_sync)'
synchronous_commit = on
CONF
as_pg "$BIN/pg_ctl -D $BASE/primary -l $BASE/logs/primary.log -w start" >/dev/null
as_pg "psql -p 5433 -qAt -c \"SELECT pg_create_physical_replication_slot('slot_sync'), pg_create_physical_replication_slot('slot_async')\"" >/dev/null

echo ">> replicas :5434 (sync) and :5435 (async, 3 s apply delay) via pg_basebackup"
for spec in "replica_sync 5434 slot_sync" "replica_async 5435 slot_async"; do
  set -- $spec
  as_pg "$BIN/pg_basebackup -d 'host=localhost port=5433 application_name=$1' -D $BASE/$1 -S $3 -X stream -R -c fast" >/dev/null
  sed -i "s/^port = 5433/port = $2/" "$BASE/$1/postgresql.conf"
  sed -i "/^synchronous_standby_names/d" "$BASE/$1/postgresql.conf"
done
echo "recovery_min_apply_delay = '3s'" >> "$BASE/replica_async/postgresql.auto.conf"
for d in replica_sync replica_async; do
  as_pg "$BIN/pg_ctl -D $BASE/$d -l $BASE/logs/$d.log -w start" >/dev/null
done

echo ">> loading the Portfolio 1 clinic schema and sample data on the primary (replicates automatically)"
as_pg "psql -p 5433 -q -f $P1/00_create_database.sql" >/dev/null 2>&1
for f in 01_schema 02_functions_triggers_views 03_seed_data; do
  as_pg "psql -p 5433 -q -v ON_ERROR_STOP=1 -d waynes_clinic -f $P1/$f.sql" >/dev/null 2>&1
done

echo ">> sharded group: coordinator :5440, shard1 :5441, shard2 :5442"
for spec in "coordinator 5440" "shard1 5441" "shard2 5442"; do
  set -- $spec
  as_pg "$BIN/initdb -D $BASE/$1 -U postgres --auth=trust -E UTF8 --locale=C.UTF-8" >/dev/null
  common_conf "$BASE/$1" "$2"
  as_pg "$BIN/pg_ctl -D $BASE/$1 -l $BASE/logs/$1.log -w start" >/dev/null
done
echo ">> waiting for both standbys to replay the clinic data (replica_async applies WAL 3 s late)"
for port in 5434 5435; do
  until as_pg "psql -p $port -d waynes_clinic -qAt -c 'SELECT count(*) FROM clinic.appointment'" >/dev/null 2>&1; do sleep 0.5; done
done
as_pg "psql -p 5433 -qAt -c \"SELECT 'replication topology ready: ' || string_agg(application_name || '=' || sync_state, ', ') FROM pg_stat_replication\""
