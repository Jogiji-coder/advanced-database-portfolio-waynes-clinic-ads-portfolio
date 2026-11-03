"""
Portfolio 3 - durability (the D in ACID): crash the server and check what survives.

1. Session A commits a stock movement.          -> must survive the crash
2. Session B inserts a row but does NOT commit. -> must disappear
3. The server is killed with an *immediate* shutdown (no checkpoint, like a power cut).
4. On restart PostgreSQL replays the write-ahead log (WAL) from the last checkpoint.

Requires control of a local test server. Override the stop/start commands with
PG_STOP_CMD / PG_START_CMD if your installation is not Debian/Ubuntu's pg_ctlcluster.
    python scripts/durability_crash_test.py
"""
import os
import subprocess
import time
from datetime import datetime

import psycopg
from session_lab import DSN, Log, Session

EVID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence")
STOP = os.environ.get("PG_STOP_CMD", "pg_ctlcluster 16 main stop -m immediate")
START = os.environ.get("PG_START_CMD", "pg_ctlcluster 16 main start")
SERVER_LOG = os.environ.get("PG_SERVER_LOG", "/var/log/postgresql/postgresql-16-main.log")

stamp = datetime.now().strftime("%H:%M:%S")
log = Log("C9  Durability - crash the server, then recover from the WAL",
          "A commits, B does not; the server is killed with an immediate shutdown (no checkpoint) and restarted.")
a, b = Session("A", log), Session("B", log)
log_len = len(open(SERVER_LOG).read().splitlines()) if os.path.exists(SERVER_LOG) else 0

a.run("BEGIN")
a.run("INSERT INTO stock_movement (medication_id, quantity, reason) VALUES (1, -30, %s) RETURNING movement_id",
      (f"Durability test {stamp}: COMMITTED",), label="INSERT INTO stock_movement ... 'COMMITTED' RETURNING movement_id")
a.run("COMMIT")
a.run("SELECT pg_current_wal_lsn() AS wal_lsn_after_commit")
b.run("BEGIN")
b.run("INSERT INTO stock_movement (medication_id, quantity, reason) VALUES (1, -99, %s) RETURNING movement_id",
      (f"Durability test {stamp}: NOT committed",), label="INSERT INTO stock_movement ... 'NOT committed'  -- left open")
log.note(f"Simulating a crash:  {STOP}")
a.conn.close(); b.conn.close()
subprocess.run(STOP, shell=True, check=True)
t0 = time.perf_counter()
subprocess.run(START, shell=True, check=True)
for _ in range(60):
    try:
        psycopg.connect(DSN).close()
        break
    except psycopg.OperationalError:
        time.sleep(0.5)
log.note(f"Server restarted and accepting connections after {time.perf_counter() - t0:.1f} s")

if os.path.exists(SERVER_LOG):
    new = open(SERVER_LOG).read().splitlines()[log_len:]
    keep = [l.split(" UTC ", 1)[-1] for l in new if any(k in l for k in
            ("interrupted", "not properly shut down", "automatic recovery", "redo starts", "redo done",
             "checkpoint complete", "ready to accept"))]
    log.add("SERVER LOG", "mon", "PostgreSQL server log during restart:\n" + "\n".join(keep[:8]))

c = Session("A", log, "session_A_after_restart")
c.run("SELECT reason, quantity FROM stock_movement WHERE reason LIKE %s ORDER BY movement_id",
      (f"Durability test {stamp}%",), label="SELECT reason, quantity FROM stock_movement WHERE reason LIKE 'Durability test%'")
rows = c.conn.execute("SELECT reason FROM stock_movement WHERE reason LIKE %s", (f"Durability test {stamp}%",)).fetchall()
ok = len(rows) == 1 and "COMMITTED" in rows[0][0] and "NOT" not in rows[0][0]
log.verdict(ok, "Only the committed row exists after the crash. The commit had been flushed to the WAL "
                "(synchronous_commit = on), so redo replayed it; B's unfinished transaction was never committed, "
                "so its row is invisible and later vacuumed away.")
c.close()
open(os.path.join(EVID, "C9_durability_crash_recovery.txt"), "w").write(log.to_text())
log.to_png(os.path.join(EVID, "C9_durability_crash_recovery.png"))
print("durability test:", "PASS" if ok else "FAIL")
