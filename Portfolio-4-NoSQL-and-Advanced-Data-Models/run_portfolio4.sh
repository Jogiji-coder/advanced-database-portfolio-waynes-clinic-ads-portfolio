#!/usr/bin/env bash
# =====================================================================
# Portfolio 4 - NoSQL and advanced data models. Needs:
#   PostgreSQL with Portfolios 1-2 loaded, MongoDB 7/8 started as a replica set
#   (transactions need one: mongod --replSet rs0, then rs.initiate()),
#   Redis 7, Python packages: psycopg pymongo redis kuzu duckdb, and mongosh.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
MONGO_URI=${MONGO_URI:-"mongodb://127.0.0.1:27017/waynes_clinic?replicaSet=rs0"}
E=evidence; R=../tools/render_terminal.py

echo ">> Document model (MongoDB)"
mongosh --quiet "$MONGO_URI" document-mongodb/01_create_collections.js
python3 document-mongodb/02_load_from_postgres.py | tee $E/mongo_02_load_output.txt
mongosh --quiet "$MONGO_URI" document-mongodb/03_core_operations.js > $E/mongo_03_core_operations_output.txt 2>&1
python3 document-mongodb/04_compare_history_latency.py > /dev/null
echo ">> Key-value model (Redis)"
python3 key-value-redis/redis_demo.py > /dev/null
echo ">> Graph model (Kuzu / Cypher)"
python3 graph-cypher/build_and_query_graph.py > /dev/null
echo ">> Columnar analytics (DuckDB / Parquet)"
python3 columnar-duckdb/columnar_analytics.py > /dev/null
echo ">> Diagram and evidence images"
dot -Tpng -Gdpi=160 diagrams/polyglot_architecture.dot -o diagrams/polyglot_architecture.png
dot -Tsvg diagrams/polyglot_architecture.dot -o diagrams/polyglot_architecture.svg
python3 $R $E/mongo_03_core_operations_output.txt $E/fig_mongo_booking.png --title "mongosh - waynes_clinic - Operation 1: book" --start "OPERATION 1" --end "OPERATION 2" --width 130
python3 $R $E/mongo_03_core_operations_output.txt $E/fig_mongo_diagnosis_txn.png --title "mongosh - waynes_clinic - Operation 2: diagnosis in a multi-document transaction" --start "OPERATION 2" --end "OPERATION 3" --width 130
python3 $R $E/mongo_03_core_operations_output.txt $E/fig_mongo_history_explain.png --title "mongosh - waynes_clinic - Operation 3: history + explain(), analytics, validation" --start "OPERATION 3" --end "COLLECTION STATS" --width 150
python3 $R $E/redis_demo_output.txt $E/fig_redis_slot_hold_cache.png --title "redis-cli - slot hold (SET NX EX) and cache-aside" --start "R1  SLOT" --end "R3  EMERGENCY" --max-lines 48 --width 150
python3 $R $E/redis_demo_output.txt $E/fig_redis_queue_streams.png --title "redis-cli - triage queue (sorted set) and critical-result stream" --start "R3  EMERGENCY" --end "R5  REAL" --width 130
python3 $R $E/graph_queries_output.txt $E/fig_graph_interactions.png --title "kuzu (Cypher) - medication interaction checks" --start "G1  MEDICATION" --end "G2  INFECTION" --width 140
python3 $R $E/graph_queries_output.txt $E/fig_graph_contact_tracing.png --title "kuzu (Cypher) - contact tracing" --start "G2  INFECTION" --end "G3  CARE" --width 140
python3 $R $E/graph_queries_output.txt $E/fig_graph_paths_similarity.png --title "kuzu (Cypher) - shortest path, similar patients, SQL comparison" --start "G5  VARIABLE" --width 140
python3 $R $E/columnar_duckdb_output.txt $E/fig_columnar_duckdb.png --title "duckdb - Parquet columnar analytics" --max-lines 70 --width 130
echo ">> Portfolio 4 done"
