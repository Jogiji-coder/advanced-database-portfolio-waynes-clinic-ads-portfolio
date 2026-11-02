#!/usr/bin/env bash
# =====================================================================
# Portfolio 2 - bulk-load the database, run the business queries and the
# optimisation benchmark, then build charts and evidence images.
# Prerequisite: Portfolio 1 has been run (./Portfolio-1-.../run_portfolio1.sh)
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
export PGHOST=${PGHOST:-localhost} PGUSER=${PGUSER:-postgres} PGPASSWORD=${PGPASSWORD:-postgres}

echo ">> Bulk load (about 2 minutes)"
psql -X -d waynes_clinic -f sql/01_bulk_load.sql > evidence/01_bulk_load_output.txt 2>&1
echo ">> Business queries"
psql -X -d waynes_clinic -f sql/02_queries.sql > results/02_query_results.txt 2>&1
if [ "${SKIP_BENCHMARK:-0}" = "1" ] && [ -f results/benchmark_results.json ]; then
  echo ">> SKIP_BENCHMARK=1: keeping the existing measurements in results/ and plans/"
  echo "   (the bulk load is deterministic - setseed - so they describe the same data)"
else
  echo ">> Optimisation benchmark (about 3 minutes)"
  rm -f plans/*.txt
  python3 scripts/benchmark.py | tee evidence/benchmark_console.txt
fi
echo ">> Applying the recommended index set"
psql -X -d waynes_clinic -f sql/03_recommended_indexes.sql > evidence/03_recommended_indexes_output.txt 2>&1
echo ">> Charts and evidence images"
python3 scripts/make_charts.py
echo ">> Portfolio 2 done"
