#!/usr/bin/env bash
# =====================================================================
# Portfolio 3 - transactions and concurrency evidence.
# Prerequisite: Portfolio 1 (and optionally Portfolio 2) has been run.
# The crash test restarts the local PostgreSQL server: only run it on a
# test machine you control (set SKIP_CRASH_TEST=1 to skip it).
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"
export PGHOST=${PGHOST:-localhost} PGUSER=${PGUSER:-postgres} PGPASSWORD=${PGPASSWORD:-postgres}

psql -X -q -v ON_ERROR_STOP=1 -d waynes_clinic -f sql/00_setup.sql > /dev/null
psql -X -d waynes_clinic -f sql/01_commit_rollback_savepoint.sql > evidence/01_commit_rollback_savepoint_output.txt 2>&1 || true
cd scripts
python3 concurrency_demos.py
if [ "${SKIP_CRASH_TEST:-0}" != "1" ]; then python3 durability_crash_test.py; fi
cd ..
R=../tools/render_terminal.py
python3 $R evidence/01_commit_rollback_savepoint_output.txt evidence/T1_T2_commit_rollback.png \
  --title "psql - waynes_clinic - T1 COMMIT and T2 ROLLBACK" --start "T1  COMMIT" --end "T3  ATOMICITY"
python3 $R evidence/01_commit_rollback_savepoint_output.txt evidence/T3_T4_error_and_savepoint.png \
  --title "psql - waynes_clinic - T3 automatic rollback and T4 SAVEPOINT" --start "T3  ATOMICITY" --end "T5  WHAT"
python3 $R evidence/01_commit_rollback_savepoint_output.txt evidence/T5_sequences.png \
  --title "psql - waynes_clinic - T5 sequences are not transactional" --start "T5  WHAT"
echo ">> Portfolio 3 done"
