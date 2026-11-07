#!/usr/bin/env bash
# =====================================================================
# Portfolio 5 - distributed databases. Builds six local PostgreSQL servers,
# runs the replication/failover and sharding demos, then a 3-member MongoDB
# replica set and a 2-shard MongoDB cluster. Run as root on a TEST machine
# (it starts, kills and restarts database servers).
# Prerequisites: PostgreSQL 16 binaries, the main clinic DB from Portfolios 1-2
# on :5432, MongoDB 7/8 binaries (set MONGO_BIN), Python psycopg + pymongo.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
E=evidence; R=../tools/render_terminal.py
scripts/01_setup_pg_nodes.sh | tee $E/01_setup_output.txt
python3 scripts/02_replication_failover_demo.py > /dev/null
python3 scripts/03_fdw_sharding_demo.py > /dev/null
python3 scripts/04_mongodb_distributed_demo.py > /dev/null 2>&1
python3 $R $E/replication_failover_log.txt $E/fig_replication_consistency.png --title "PostgreSQL replication - topology, consistency, read-only standbys, commit latency" --start "D1  TOPOLOGY" --end "D5  AVAIL" --width 160
python3 $R $E/replication_failover_log.txt $E/fig_replication_availability_failover.png --title "PostgreSQL replication - availability trade-off, failover, pg_rewind rejoin" --start "D5  AVAIL" --width 160
python3 $R $E/sharding_fdw_log.txt $E/fig_sharding_build_distribution.png --title "PostgreSQL sharding (postgres_fdw) - routing, distribution, single-shard query" --start "S1  BUILD" --end "S4  CROSS" --width 165
python3 $R $E/sharding_fdw_log.txt $E/fig_sharding_queries_failure.png --title "PostgreSQL sharding - cross-shard queries, routed writes, shard failure" --start "S4  CROSS" --width 165
python3 $R $E/mongodb_distributed_log.txt $E/fig_mongodb_replicaset_failover.png --title "MongoDB replica set - write concern, read preference, automatic failover" --start "M1  REPLICA" --end "M5  SHARDED" --width 150
python3 $R $E/mongodb_distributed_log.txt $E/fig_mongodb_sharding.png --title "MongoDB sharded cluster - hashed shard key and query routing" --start "M5  SHARDED" --width 150
echo ">> Portfolio 5 done"
