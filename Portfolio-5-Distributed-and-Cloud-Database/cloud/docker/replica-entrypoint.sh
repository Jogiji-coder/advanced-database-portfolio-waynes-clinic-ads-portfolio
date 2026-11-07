#!/bin/bash
# Turns an empty official postgres:16 container into a streaming standby of pg-primary.
# Usage (compose): replica-entrypoint.sh <application_name> <slot_name> [apply_delay]
set -e
APP="$1"; SLOT="$2"; DELAY="${3:-0}"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h pg-primary -U postgres -q; do sleep 1; done
  mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA" && chmod 700 "$PGDATA"
  gosu postgres pg_basebackup -h pg-primary -U replicator -D "$PGDATA" -X stream -R -c fast \
       -d "application_name=$APP" -S "$SLOT"
  [ "$DELAY" != "0" ] && echo "recovery_min_apply_delay = '$DELAY'" >> "$PGDATA/postgresql.auto.conf"
fi
exec gosu postgres postgres -c hot_standby=on -c wal_receiver_status_interval=1s
