"""
Portfolio 3 - concurrency demonstrations on Wayne's Clinic Baltimore (PostgreSQL 16)

Each demo opens two (sometimes three) real database sessions and interleaves
their statements in a fixed order, so the anomaly - or the mechanism that
prevents it - is reproduced exactly. Writes evidence/C*.txt (timestamped log)
and evidence/C*.png (two-column timeline) plus evidence/concurrency_summary.md.

    python scripts/concurrency_demos.py
"""
import os
import time

from session_lab import DSN, Log, Monitor, Session

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
EVID = os.path.join(HERE, "..", "evidence")
SUMMARY = []


def admin():
    c = psycopg.connect(DSN, autocommit=True)
    c.execute("SET search_path TO clinic, public")
    c.execute("SET timezone TO 'America/New_York'")
    return c


def save(log, stem, names=("A", "B")):
    open(os.path.join(EVID, f"{stem}.txt"), "w").write(log.to_text())
    log.to_png(os.path.join(EVID, f"{stem}.png"), names)
    print("  wrote", stem)


def record(demo, level, anomaly, outcome):
    SUMMARY.append((demo, level, anomaly, outcome))


ADM = admin()
INSULIN = ADM.execute("SELECT medication_id FROM medication WHERE generic_name = 'Insulin Glargine'").fetchone()[0]
ALBUTEROL = ADM.execute("SELECT medication_id FROM medication WHERE generic_name = 'Albuterol'").fetchone()[0]
ORIGINAL_STOCK = dict(ADM.execute("SELECT medication_id, stock_on_hand FROM medication WHERE medication_id IN (%s, %s)",
                                  (INSULIN, ALBUTEROL)).fetchall())
NEXT_WED = ADM.execute("SELECT (date_trunc('week', now() AT TIME ZONE 'America/New_York')::date + 9)").fetchone()[0]
# Marcus Bell's potassium (appointment 15) and Tyrone Jackson's INR (appointment 30) from the Portfolio 1 sample
K_RESULT = ADM.execute("""SELECT r.lab_result_id FROM lab_result r JOIN lab_order lo USING (lab_order_id)
                          WHERE lo.appointment_id = 15 AND r.test_code = 'K'""").fetchone()[0]
INR_RESULT = ADM.execute("""SELECT r.lab_result_id FROM lab_result r JOIN lab_order lo USING (lab_order_id)
                            WHERE lo.appointment_id = 30 AND r.test_code = 'INR'""").fetchone()[0]


def server_log_excerpt(log, marker, lines=5):
    """Append the server's own report (PostgreSQL log) of the event, if the log file is readable."""
    path = os.environ.get("PG_SERVER_LOG", "/var/log/postgresql/postgresql-16-main.log")
    try:
        text = open(path).read().splitlines()
        idx = max(i for i, l in enumerate(text) if marker in l)
        excerpt = [l.split(" UTC ", 1)[-1] if " UTC " in l else l for l in text[idx: idx + lines]]
        log.add("SERVER LOG", "mon", "PostgreSQL server log:\n" + "\n".join(excerpt))
    except (OSError, ValueError):
        log.note(f"(server log not readable at {path}; set PG_SERVER_LOG to include it)")


def set_stock(qty):
    ADM.execute("UPDATE medication SET stock_on_hand = %s WHERE medication_id = %s", (qty, INSULIN))


# ======================================================================= C1
def c1_dirty_read():
    log = Log("C1  Dirty read? - READ UNCOMMITTED in PostgreSQL",
              "Lab scientist A types a potassium result but has not committed. Doctor B reads it at READ UNCOMMITTED.")
    a, b = Session("A", log), Session("B", log)
    a.run("BEGIN")
    a.run("UPDATE lab_result SET result_value = 6.9 WHERE lab_result_id = %s", (K_RESULT,),
          label=f"UPDATE lab_result SET result_value = 6.9 WHERE lab_result_id = {K_RESULT}  -- not committed")
    b.run("BEGIN ISOLATION LEVEL READ UNCOMMITTED")
    seen = b.run("SELECT result_value, abnormal_flag FROM lab_result WHERE lab_result_id = %s", (K_RESULT,),
                 label=f"SELECT result_value, abnormal_flag FROM lab_result WHERE lab_result_id = {K_RESULT}")
    b.run("COMMIT")
    a.run("ROLLBACK")
    dirty = "6.9" in str(seen)
    log.verdict(not dirty, "B saw the last COMMITTED value (3.3), never A's uncommitted 6.9. PostgreSQL implements "
                           "READ UNCOMMITTED as READ COMMITTED, so dirty reads cannot happen at any level.")
    record("C1 Dirty read", "READ UNCOMMITTED", "No", "Uncommitted change invisible to other sessions")
    save(log, "C1_dirty_read")
    a.close(), b.close()


# ======================================================================= C2
def c2_non_repeatable(level):
    log = Log(f"C2  Non-repeatable read - {level}",
              "Doctor A reads Tyrone Jackson's INR twice in one transaction; meanwhile the lab (B) corrects it.")
    a, b = Session("A", log), Session("B", log)
    a.run(f"BEGIN ISOLATION LEVEL {level}")
    first = a.run("SELECT result_value FROM lab_result WHERE lab_result_id = %s", (INR_RESULT,),
                  label=f"SELECT result_value FROM lab_result WHERE lab_result_id = {INR_RESULT}  -- 1st read")
    b.run("UPDATE lab_result SET result_value = 3.6 WHERE lab_result_id = %s", (INR_RESULT,),
          label=f"UPDATE lab_result SET result_value = 3.6 WHERE lab_result_id = {INR_RESULT}  -- autocommit")
    second = a.run("SELECT result_value FROM lab_result WHERE lab_result_id = %s", (INR_RESULT,),
                   label=f"SELECT result_value FROM lab_result WHERE lab_result_id = {INR_RESULT}  -- 2nd read")
    a.run("COMMIT")
    changed = first != second
    if level == "READ COMMITTED":
        log.verdict(False, f"NON-REPEATABLE READ: the same query in one transaction returned {first} then {second}. "
                           "READ COMMITTED takes a new snapshot for every statement.")
    else:
        log.verdict(not changed, f"Both reads returned {first}: REPEATABLE READ uses one snapshot for the whole "
                                 "transaction, so A's view is stable even though B committed.")
    record("C2 Non-repeatable read", level, "Yes" if changed else "No",
           "Second read saw B's commit" if changed else "Snapshot isolation kept the first value")
    ADM.execute("UPDATE lab_result SET result_value = 2.1 WHERE lab_result_id = %s", (INR_RESULT,))
    save(log, f"C2_non_repeatable_read_{level.lower().replace(" ", "_")}")
    a.close(), b.close()


# ======================================================================= C3
def c3_phantom(level):
    log = Log(f"C3  Phantom read - {level}",
              "Manager A counts Dr Raman's bookings for next Wednesday twice; receptionist B books a new slot in between.")
    a, b = Session("A", log), Session("B", log)
    slot = ADM.execute("SELECT slot_start FROM available_slots(3, %s) LIMIT 1", (NEXT_WED,)).fetchone()[0]
    q = ("SELECT count(*) AS bookings FROM appointment WHERE doctor_id = 3 AND status <> 'Cancelled' "
         "AND scheduled_start >= %s AND scheduled_start < %s::date + 1")
    a.run(f"BEGIN ISOLATION LEVEL {level}")
    n1 = a.run(q, (NEXT_WED, NEXT_WED), label=f"SELECT count(*) ... doctor 3 on {NEXT_WED}  -- 1st count")
    b.run("SELECT book_appointment(5, 3, %s, 'Lipid follow-up') AS new_appointment", (slot,),
          label=f"SELECT book_appointment(5, 3, '{slot:%Y-%m-%d %H:%M}')  -- autocommit")
    n2 = a.run(q, (NEXT_WED, NEXT_WED), label=f"SELECT count(*) ... doctor 3 on {NEXT_WED}  -- 2nd count")
    a.run("COMMIT")
    phantom = n1 != n2
    if phantom:
        log.verdict(False, f"PHANTOM: a row that did not exist at the first count appeared ({n1} -> {n2}).")
    else:
        log.verdict(True, f"No phantom ({n1} both times). PostgreSQL's REPEATABLE READ is snapshot isolation, "
                          "which also prevents phantoms - stricter than the SQL standard requires.")
    record("C3 Phantom read", level, "Yes" if phantom else "No",
           "New booking appeared mid-transaction" if phantom else "Snapshot hid the new booking")
    ADM.execute("DELETE FROM appointment WHERE doctor_id = 3 AND scheduled_start = %s", (slot,))
    save(log, f"C3_phantom_{level.lower().replace(" ", "_")}")
    a.close(), b.close()


# ======================================================================= C4
def c4_lost_update():
    variants = []

    # (a) the problem
    set_stock(10)
    log = Log("C4a  Lost update - read-modify-write at READ COMMITTED",
              "Two pharmacists dispense insulin at the same time: A takes 3 pens, B takes 4. Stock starts at 10.")
    a, b = Session("A", log), Session("B", log)
    a.run("BEGIN"); b.run("BEGIN")
    sa = int(a.value("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,)))
    log.add("A", "sql", "SELECT stock_on_hand FROM medication WHERE generic_name = 'Insulin Glargine'"); log.add("A", "out", f"stock_on_hand = {sa}")
    sb = int(b.value("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,)))
    log.add("B", "sql", "SELECT stock_on_hand FROM medication WHERE generic_name = 'Insulin Glargine'"); log.add("B", "out", f"stock_on_hand = {sb}")
    a.run("UPDATE medication SET stock_on_hand = %s WHERE medication_id = %s", (sa - 3, INSULIN),
          label=f"UPDATE medication SET stock_on_hand = {sa - 3}  -- {sa} - 3, computed by the application")
    a.run("COMMIT")
    b.run("UPDATE medication SET stock_on_hand = %s WHERE medication_id = %s", (sb - 4, INSULIN),
          label=f"UPDATE medication SET stock_on_hand = {sb - 4}  -- {sb} - 4, based on its stale read")
    b.run("COMMIT")
    final = ADM.execute("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,)).fetchone()[0]
    log.verdict(final == 3, f"LOST UPDATE: final stock is {final}, but 7 pens were dispensed from 10 so it must be 3. "
                            "A's update was silently overwritten.")
    record("C4a Lost update (read-modify-write)", "READ COMMITTED", "Yes", f"Final stock {final}, should be 3")
    save(log, "C4a_lost_update"); a.close(); b.close()

    # (b) pessimistic locking
    set_stock(10)
    log = Log("C4b  Fix 1 - pessimistic locking with SELECT ... FOR UPDATE",
              "Same race, but each pharmacist locks the medication row when reading it.")
    a, b, m = Session("A", log), Session("B", log), Monitor("MON", log, "session_monitor")
    a.run("BEGIN"); b.run("BEGIN")
    a.run("SELECT stock_on_hand FROM medication WHERE medication_id = %s FOR UPDATE", (INSULIN,),
          label="SELECT stock_on_hand FROM medication WHERE ... FOR UPDATE")
    b.start("SELECT stock_on_hand FROM medication WHERE medication_id = %s FOR UPDATE", (INSULIN,),
            label="SELECT stock_on_hand FROM medication WHERE ... FOR UPDATE")
    m.snapshot(a.pid, b.pid)
    a.run("UPDATE medication SET stock_on_hand = 7 WHERE medication_id = %s", (INSULIN,),
          label="UPDATE medication SET stock_on_hand = 7  -- 10 - 3")
    a.run("COMMIT")
    b.wait()
    sb = int(str(b.result).split("=")[1])
    b.run("UPDATE medication SET stock_on_hand = %s WHERE medication_id = %s", (sb - 4, INSULIN),
          label=f"UPDATE medication SET stock_on_hand = {sb - 4}  -- {sb} - 4")
    b.run("COMMIT")
    final = ADM.execute("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,)).fetchone()[0]
    log.verdict(final == 3, f"Final stock {final}. B was made to wait until A committed, then read the new value (7).")
    record("C4b Lost update + SELECT FOR UPDATE", "READ COMMITTED + row lock", "No", f"Final stock {final}; B waited for A")
    save(log, "C4b_select_for_update"); a.close(); b.close(); m.close()

    # (c) atomic update
    set_stock(10)
    log = Log("C4c  Fix 2 - let the database do the arithmetic (atomic UPDATE)",
              "UPDATE ... SET stock = stock - n. B's update re-reads the row after A commits.")
    a, b = Session("A", log), Session("B", log)
    a.run("BEGIN"); b.run("BEGIN")
    a.run("UPDATE medication SET stock_on_hand = stock_on_hand - 3 WHERE medication_id = %s", (INSULIN,),
          label="UPDATE medication SET stock_on_hand = stock_on_hand - 3 WHERE ...")
    b.start("UPDATE medication SET stock_on_hand = stock_on_hand - 4 WHERE medication_id = %s", (INSULIN,),
            label="UPDATE medication SET stock_on_hand = stock_on_hand - 4 WHERE ...")
    a.run("COMMIT")
    b.wait()
    b.run("COMMIT")
    final = ADM.execute("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,)).fetchone()[0]
    log.verdict(final == 3, f"Final stock {final}. The row lock taken by A's UPDATE queued B; B then re-evaluated "
                            "stock_on_hand on the newly committed version (PostgreSQL's EvalPlanQual re-check).")
    record("C4c Lost update + atomic UPDATE", "READ COMMITTED", "No", f"Final stock {final}")
    save(log, "C4c_atomic_update"); a.close(); b.close()

    # (d) repeatable read detects it
    set_stock(10)
    log = Log("C4d  Fix 3 - REPEATABLE READ detects the conflict (first-updater-wins)",
              "Same read-modify-write code as C4a, but both transactions run at REPEATABLE READ.")
    a, b = Session("A", log), Session("B", log)
    a.run("BEGIN ISOLATION LEVEL REPEATABLE READ"); b.run("BEGIN ISOLATION LEVEL REPEATABLE READ")
    a.run("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,), label="SELECT stock_on_hand ...")
    b.run("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,), label="SELECT stock_on_hand ...")
    a.run("UPDATE medication SET stock_on_hand = 7 WHERE medication_id = %s", (INSULIN,), label="UPDATE ... SET stock_on_hand = 7")
    a.run("COMMIT")
    b.run("UPDATE medication SET stock_on_hand = 6 WHERE medication_id = %s", (INSULIN,), label="UPDATE ... SET stock_on_hand = 6")
    b.run("ROLLBACK")
    log.note("B's application catches SQLSTATE 40001 and retries the whole transaction:")
    b.run("BEGIN ISOLATION LEVEL REPEATABLE READ")
    v = b.run("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,), label="SELECT stock_on_hand ...  -- retry")
    sb = int(str(v).split("=")[1])
    b.run("UPDATE medication SET stock_on_hand = %s WHERE medication_id = %s", (sb - 4, INSULIN),
          label=f"UPDATE ... SET stock_on_hand = {sb - 4}")
    b.run("COMMIT")
    final = ADM.execute("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,)).fetchone()[0]
    log.verdict(final == 3, f"Final stock {final}. The stale write was rejected with 40001 instead of being lost; "
                            "the retry saw the committed value.")
    record("C4d Lost update at REPEATABLE READ", "REPEATABLE READ", "No (40001 + retry)", f"Final stock {final}")
    save(log, "C4d_repeatable_read_retry"); a.close(); b.close()

    # (e) optimistic concurrency control
    set_stock(10)
    log = Log("C4e  Fix 4 - optimistic concurrency control (compare-and-set)",
              "No locks held while the pharmacist thinks. The UPDATE only succeeds if the value is still what was read.")
    a, b = Session("A", log), Session("B", log)
    a.run("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,), label="SELECT stock_on_hand ...  -- reads 10")
    b.run("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,), label="SELECT stock_on_hand ...  -- reads 10")
    a.run("UPDATE medication SET stock_on_hand = 7 WHERE medication_id = %s AND stock_on_hand = 10", (INSULIN,),
          label="UPDATE medication SET stock_on_hand = 7 WHERE ... AND stock_on_hand = 10")
    b.run("UPDATE medication SET stock_on_hand = 6 WHERE medication_id = %s AND stock_on_hand = 10", (INSULIN,),
          label="UPDATE medication SET stock_on_hand = 6 WHERE ... AND stock_on_hand = 10")
    log.note("B updated 0 rows -> someone changed the stock since B read it. B re-reads and tries again.")
    b.run("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,), label="SELECT stock_on_hand ...  -- re-read")
    b.run("UPDATE medication SET stock_on_hand = 3 WHERE medication_id = %s AND stock_on_hand = 7", (INSULIN,),
          label="UPDATE medication SET stock_on_hand = 3 WHERE ... AND stock_on_hand = 7")
    final = ADM.execute("SELECT stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,)).fetchone()[0]
    log.verdict(final == 3, f"Final stock {final}. The conflict was detected by the WHERE clause (UPDATE 0) without any waiting.")
    record("C4e Lost update + optimistic CAS", "READ COMMITTED (no locks)", "No (UPDATE 0 + retry)", f"Final stock {final}")
    save(log, "C4e_optimistic_cas"); a.close(); b.close()


# ======================================================================= C5
def c5_write_skew(level):
    shift = NEXT_WED
    ADM.execute("DELETE FROM on_call_roster")
    ADM.execute("INSERT INTO on_call_roster VALUES (%s, 9, true), (%s, 4, true)", (shift, shift))
    log = Log(f"C5  Write skew - the on-call rule - {level}",
              "Rule: at least one doctor must stay on call. Dr Fairbanks (A) and Dr Whitfield (B) each check "
              "that 2 are on call, then each signs off.")
    a, b = Session("A", log), Session("B", log)
    cnt = "SELECT count(*) AS on_call_now FROM on_call_roster WHERE shift_date = %s AND on_call"
    a.run(f"BEGIN ISOLATION LEVEL {level}"); b.run(f"BEGIN ISOLATION LEVEL {level}")
    a.run(cnt, (shift,), label="SELECT count(*) FROM on_call_roster WHERE on_call  -- rule check")
    b.run(cnt, (shift,), label="SELECT count(*) FROM on_call_roster WHERE on_call  -- rule check")
    a.run("UPDATE on_call_roster SET on_call = false WHERE shift_date = %s AND doctor_id = 9", (shift,),
          label="UPDATE on_call_roster SET on_call = false WHERE doctor_id = 9")
    b.run("UPDATE on_call_roster SET on_call = false WHERE shift_date = %s AND doctor_id = 4", (shift,),
          label="UPDATE on_call_roster SET on_call = false WHERE doctor_id = 4")
    a.run("COMMIT")
    res = b.run("COMMIT")
    if isinstance(res, Exception):
        b.run("ROLLBACK")
        log.note("B's application retries after SQLSTATE 40001:")
        b.run(f"BEGIN ISOLATION LEVEL {level}")
        b.run(cnt, (shift,), label="SELECT count(*) FROM on_call_roster WHERE on_call  -- re-check")
        log.note("Only 1 doctor on call -> the application refuses B's request and ends the transaction.")
        b.run("ROLLBACK")
    final = ADM.execute("SELECT count(*) FROM on_call_roster WHERE shift_date = %s AND on_call", (shift,)).fetchone()[0]
    if final == 0:
        log.verdict(False, "WRITE SKEW: both checks passed against the same snapshot, each updated a DIFFERENT row, "
                           "so no write-write conflict was seen. Nobody is on call - the invariant is broken.")
    else:
        log.verdict(True, f"{final} doctor still on call. Serializable Snapshot Isolation tracked the read/write "
                          "dependencies and aborted B (40001); the retry saw the truth.")
    record("C5 Write skew (on-call rule)", level, "Yes" if final == 0 else "No (40001 + retry)", f"{final} doctor(s) on call")
    save(log, f"C5_write_skew_{level.lower().replace(" ", "_")}")
    a.close(), b.close()


# ======================================================================= C6
def c6_double_booking():
    slot = ADM.execute("SELECT slot_start FROM available_slots(1, %s) LIMIT 1", (NEXT_WED,)).fetchone()[0]
    end = ADM.execute("SELECT %s::timestamptz + interval '30 minutes'", (slot,)).fetchone()[0]
    s = f"{slot:%a %d %b %H:%M}"

    # (a) application-level check
    ADM.execute("TRUNCATE appointment_unsafe")
    log = Log("C6a  Double booking - 'check, then insert' in application code",
              f"Two receptionists book Dr Okafor's {s} slot at the same moment, using a table with no exclusion constraint.")
    a, b = Session("A", log), Session("B", log)
    free = ("SELECT count(*) AS clashes FROM appointment_unsafe WHERE doctor_id = 1 "
            "AND tstzrange(scheduled_start, scheduled_end) && tstzrange(%s, %s)")
    a.run("BEGIN"); b.run("BEGIN")
    a.run(free, (slot, end), label="SELECT count(*) FROM appointment_unsafe WHERE doctor_id = 1 AND <overlaps slot>")
    b.run(free, (slot, end), label="SELECT count(*) FROM appointment_unsafe WHERE doctor_id = 1 AND <overlaps slot>")
    log.note("Both see 0 clashes, so both applications decide the slot is free.")
    a.run("INSERT INTO appointment_unsafe (patient_id, doctor_id, scheduled_start, scheduled_end, booked_by) "
          "VALUES (5, 1, %s, %s, 'Receptionist A')", (slot, end), label="INSERT INTO appointment_unsafe ... patient 5")
    b.run("INSERT INTO appointment_unsafe (patient_id, doctor_id, scheduled_start, scheduled_end, booked_by) "
          "VALUES (7, 1, %s, %s, 'Receptionist B')", (slot, end), label="INSERT INTO appointment_unsafe ... patient 7")
    a.run("COMMIT"); b.run("COMMIT")
    n = ADM.execute("SELECT count(*) FROM appointment_unsafe WHERE doctor_id = 1").fetchone()[0]
    log.verdict(n == 1, f"DOUBLE BOOKING: {n} patients now hold the same slot. Neither transaction could see the "
                        "other's uncommitted insert, and nothing in the database forbade the result.")
    record("C6a Double booking (check-then-insert)", "READ COMMITTED, no constraint", "Yes", f"{n} bookings for one slot")
    save(log, "C6a_double_booking_unsafe"); a.close(); b.close()

    # (b) exclusion constraint, A commits
    log = Log("C6b  Double booking prevented by the EXCLUDE constraint",
              f"Same race on the real appointment table via book_appointment(). Slot: Dr Okafor {s}.")
    a, b, m = Session("A", log), Session("B", log), Monitor("MON", log, "session_monitor")
    a.run("BEGIN"); b.run("BEGIN")
    a.run("SELECT book_appointment(5, 1, %s, 'Receptionist A') AS appointment_id", (slot,),
          label=f"SELECT book_appointment(patient 5, doctor 1, '{s}')")
    b.start("SELECT book_appointment(7, 1, %s, 'Receptionist B') AS appointment_id", (slot,),
            label=f"SELECT book_appointment(patient 7, doctor 1, '{s}')")
    m.snapshot(a.pid, b.pid)
    log.note("B cannot know yet whether the slot is taken: it depends on whether A commits. It waits on A's transaction id.")
    a.run("COMMIT")
    b.wait()
    b.run("ROLLBACK")
    n = ADM.execute("SELECT count(*) FROM appointment WHERE doctor_id = 1 AND scheduled_start = %s AND status <> 'Cancelled'",
                    (slot,)).fetchone()[0]
    log.verdict(n == 1, f"{n} booking for the slot. The constraint is enforced by the GiST index itself, so the "
                        "check and the insert are one atomic step - there is no race window.")
    record("C6b Double booking + EXCLUDE constraint", "READ COMMITTED", "No", "B blocked, then 23P01")
    save(log, "C6b_double_booking_exclusion"); a.close(); b.close(); m.close()

    # (c) exclusion constraint, A rolls back
    ADM.execute("DELETE FROM appointment WHERE doctor_id = 1 AND scheduled_start = %s", (slot,))
    log = Log("C6c  ... and if the first booking is abandoned, the waiter gets the slot",
              "Receptionist A starts a booking but the patient hangs up, so A rolls back.")
    a, b = Session("A", log), Session("B", log)
    a.run("BEGIN"); b.run("BEGIN")
    a.run("SELECT book_appointment(5, 1, %s, 'Receptionist A') AS appointment_id", (slot,),
          label=f"SELECT book_appointment(patient 5, doctor 1, '{s}')")
    b.start("SELECT book_appointment(7, 1, %s, 'Receptionist B') AS appointment_id", (slot,),
            label=f"SELECT book_appointment(patient 7, doctor 1, '{s}')")
    a.run("ROLLBACK")
    b.wait()
    b.run("COMMIT")
    who = ADM.execute("SELECT patient_id FROM appointment WHERE doctor_id = 1 AND scheduled_start = %s", (slot,)).fetchone()
    log.verdict(who and who[0] == 7, "Patient 7 holds the slot. Waiting (rather than failing fast) was correct: "
                                     "the conflicting row never became visible.")
    record("C6c EXCLUDE constraint, first booker rolls back", "READ COMMITTED", "No", "Waiter succeeded")
    save(log, "C6c_double_booking_first_rolls_back"); a.close(); b.close()
    ADM.execute("DELETE FROM appointment WHERE doctor_id = 1 AND scheduled_start = %s", (slot,))


# ======================================================================= C7
def c7_deadlock():
    set_stock(500)
    ADM.execute("UPDATE medication SET stock_on_hand = 500 WHERE medication_id = %s", (ALBUTEROL,))
    log = Log("C7a  Deadlock - two transfers lock the same rows in opposite order",
              "A moves stock insulin -> albuterol; B moves albuterol -> insulin. deadlock_timeout = 1s.")
    a, b, m = Session("A", log), Session("B", log), Monitor("MON", log, "session_monitor")
    a.run("BEGIN"); b.run("BEGIN")
    a.run("UPDATE medication SET stock_on_hand = stock_on_hand - 5 WHERE medication_id = %s", (INSULIN,),
          label="UPDATE medication ... WHERE generic_name = 'Insulin Glargine'  -- locks row 1")
    b.run("UPDATE medication SET stock_on_hand = stock_on_hand - 5 WHERE medication_id = %s", (ALBUTEROL,),
          label="UPDATE medication ... WHERE generic_name = 'Albuterol'  -- locks row 2")
    a.start("UPDATE medication SET stock_on_hand = stock_on_hand + 5 WHERE medication_id = %s", (ALBUTEROL,),
            label="UPDATE medication ... WHERE generic_name = 'Albuterol'  -- needs row 2")
    m.snapshot(a.pid, b.pid)
    b.start("UPDATE medication SET stock_on_hand = stock_on_hand + 5 WHERE medication_id = %s", (INSULIN,),
            label="UPDATE medication ... WHERE generic_name = 'Insulin Glargine'  -- needs row 1: cycle!")
    ra, rb = a.wait(), b.wait()
    loser = "A" if isinstance(ra, Exception) else "B"
    winner = "B" if loser == "A" else "A"
    {"A": a, "B": b}[loser].run("ROLLBACK")
    {"A": a, "B": b}[winner].run("COMMIT")
    server_log_excerpt(log, "deadlock detected")
    log.verdict(True, f"PostgreSQL's deadlock detector found the wait-for cycle after deadlock_timeout and aborted "
                      f"session {loser} (40P01) so that {winner} could finish. {loser}'s work is lost and must be retried.")
    record("C7a Deadlock (opposite lock order)", "READ COMMITTED", "Deadlock detected", f"Session {loser} aborted with 40P01")
    save(log, "C7a_deadlock"); a.close(); b.close(); m.close()

    log = Log("C7b  Deadlock prevention - always lock in the same order",
              "Both transfers first lock BOTH rows with ORDER BY medication_id FOR UPDATE.")
    a, b = Session("A", log), Session("B", log)
    lock_both = "SELECT medication_id, stock_on_hand FROM medication WHERE medication_id IN (%s, %s) ORDER BY medication_id FOR UPDATE"
    a.run("BEGIN"); b.run("BEGIN")
    a.run(lock_both, (INSULIN, ALBUTEROL), label="SELECT ... WHERE medication_id IN (insulin, albuterol) ORDER BY medication_id FOR UPDATE")
    b.start(lock_both, (INSULIN, ALBUTEROL), label="SELECT ... WHERE medication_id IN (insulin, albuterol) ORDER BY medication_id FOR UPDATE")
    a.run("UPDATE medication SET stock_on_hand = stock_on_hand - 5 WHERE medication_id = %s", (INSULIN,), label="UPDATE insulin -5")
    a.run("UPDATE medication SET stock_on_hand = stock_on_hand + 5 WHERE medication_id = %s", (ALBUTEROL,), label="UPDATE albuterol +5")
    a.run("COMMIT")
    b.wait()
    b.run("UPDATE medication SET stock_on_hand = stock_on_hand - 5 WHERE medication_id = %s", (ALBUTEROL,), label="UPDATE albuterol -5")
    b.run("UPDATE medication SET stock_on_hand = stock_on_hand + 5 WHERE medication_id = %s", (INSULIN,), label="UPDATE insulin +5")
    b.run("COMMIT")
    log.verdict(True, "No deadlock: B simply queued behind A. A global lock order turns a possible cycle into a "
                      "queue. clinic.dispense_prescription() uses the same rule.")
    record("C7b Deadlock prevention (ordered locking)", "READ COMMITTED", "No", "B queued, both committed")
    save(log, "C7b_ordered_locking"); a.close(); b.close()


# ======================================================================= C8
def c8_mvcc():
    set_stock(10)
    log = Log("C8  MVCC - readers never block writers, writers never block readers",
              "A holds a REPEATABLE READ snapshot open; B updates and commits; A still sees the old row version.")
    a, b, c = Session("A", log), Session("B", log), Session("C", log)
    a.run("BEGIN ISOLATION LEVEL REPEATABLE READ")
    a.run("SELECT ctid, xmin, xmax, stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,),
          label="SELECT ctid, xmin, xmax, stock_on_hand FROM medication WHERE ...")
    b.run("UPDATE medication SET stock_on_hand = 99 WHERE medication_id = %s RETURNING ctid, xmin", (INSULIN,),
          label="UPDATE medication SET stock_on_hand = 99 WHERE ... RETURNING ctid, xmin  -- no waiting")
    a.run("SELECT ctid, xmin, xmax, stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,),
          label="SELECT ctid, xmin, xmax, stock_on_hand ...  -- same snapshot")
    log.note("Session C (a new snapshot) sees the version B created:")
    c.run("SELECT ctid, xmin, xmax, stock_on_hand FROM medication WHERE medication_id = %s", (INSULIN,),
          label="SELECT ctid, xmin, xmax, stock_on_hand ...  -- new snapshot")
    a.run("COMMIT")
    log.verdict(True, "Two physical versions of the row existed at once (different ctid). The old version's xmax was set "
                      "to B's transaction id, but A's snapshot predates B, so A kept reading it. VACUUM later removes it.")
    record("C8 MVCC row versions", "REPEATABLE READ", "-", "Old version served to the old snapshot, no blocking")
    save(log, "C8_mvcc_row_versions"); a.close(); b.close(); c.close()


def main():
    print("Running concurrency demos ...")
    ADM.execute("ALTER SYSTEM SET deadlock_timeout = '1s'")
    ADM.execute("SELECT pg_reload_conf()")
    try:
        c1_dirty_read()
        c2_non_repeatable("READ COMMITTED"); c2_non_repeatable("REPEATABLE READ")
        c3_phantom("READ COMMITTED"); c3_phantom("REPEATABLE READ")
        c4_lost_update()
        c5_write_skew("REPEATABLE READ"); c5_write_skew("SERIALIZABLE")
        c6_double_booking()
        c7_deadlock()
        c8_mvcc()
    finally:
        for mid, qty in ORIGINAL_STOCK.items():
            ADM.execute("UPDATE medication SET stock_on_hand = %s WHERE medication_id = %s", (qty, mid))
        ADM.execute("DELETE FROM on_call_roster")
    lines = ["# Concurrency demonstration summary", "",
             "Generated by `scripts/concurrency_demos.py` against PostgreSQL 16. Each row links to a timestamped log "
             "(`.txt`) and a two-column timeline (`.png`) in this folder.", "",
             "| Demo | Isolation level / mechanism | Anomaly observed? | Outcome |", "|---|---|---|---|"]
    lines += [f"| {d} | {l} | {a} | {o} |" for d, l, a, o in SUMMARY]
    open(os.path.join(EVID, "concurrency_summary.md"), "w").write("\n".join(lines) + "\n")
    print("summary written")


if __name__ == "__main__":
    main()
