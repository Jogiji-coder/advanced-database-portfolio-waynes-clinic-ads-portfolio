-- =====================================================================
-- Wayne's Clinic Baltimore — Advanced Database Systems Portfolio
-- Portfolio 3 · Script 00 · Support objects for the transaction demos
-- Run after Portfolio 1 (and optionally Portfolio 2):
--     psql -d waynes_clinic -f 00_setup.sql
-- =====================================================================
\set ON_ERROR_STOP 1
SET search_path TO clinic, public;

-- ---------------------------------------------------------------------
-- On-call roster. Business rule: at least ONE emergency doctor must be on
-- call for every night shift. The rule spans several rows, so no CHECK
-- constraint can enforce it - it is exactly the kind of invariant that
-- breaks under "write skew" unless SERIALIZABLE isolation is used.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS on_call_roster;
CREATE TABLE on_call_roster (
    shift_date date    NOT NULL,
    doctor_id  integer NOT NULL REFERENCES doctor (doctor_id),
    on_call    boolean NOT NULL DEFAULT true,
    PRIMARY KEY (shift_date, doctor_id)
);
COMMENT ON TABLE on_call_roster IS 'Night-shift on-call roster; invariant: >= 1 doctor on call per shift';

-- ---------------------------------------------------------------------
-- A copy of appointment WITHOUT the exclusion constraints, used only to
-- show what goes wrong when double booking is prevented in application
-- code ("check, then insert") instead of by the database.
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS appointment_unsafe;
CREATE TABLE appointment_unsafe (
    appointment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_id      integer NOT NULL REFERENCES patient (patient_id),
    doctor_id       integer NOT NULL REFERENCES doctor (doctor_id),
    scheduled_start timestamptz NOT NULL,
    scheduled_end   timestamptz NOT NULL,
    booked_by       text
);

-- ---------------------------------------------------------------------
-- Audit trail for stock movements (shows that the audit row and the stock
-- change commit or roll back together).
-- ---------------------------------------------------------------------
DROP TABLE IF EXISTS stock_movement;
CREATE TABLE stock_movement (
    movement_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    medication_id integer NOT NULL REFERENCES medication (medication_id),
    quantity      integer NOT NULL,
    reason        text    NOT NULL,
    moved_at      timestamptz NOT NULL DEFAULT now(),
    moved_by      text    NOT NULL DEFAULT current_user
);

-- ---------------------------------------------------------------------
-- dispense_prescription(): the pharmacy's "safe" dispensing routine.
-- Locks the medication rows in a fixed order (ascending medication_id) to
-- avoid deadlocks, checks stock, decrements it and writes the audit rows,
-- all inside the caller's transaction.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dispense_prescription(p_prescription_id bigint)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    r       record;
    v_lines integer := 0;
BEGIN
    FOR r IN
        SELECT pi.medication_id, pi.quantity, m.stock_on_hand, m.generic_name
        FROM prescription_item pi
        JOIN medication m ON m.medication_id = pi.medication_id
        WHERE pi.prescription_id = p_prescription_id
        ORDER BY pi.medication_id                -- consistent lock order
        FOR UPDATE OF m                          -- pessimistic row locks
    LOOP
        IF r.stock_on_hand < r.quantity THEN
            RAISE EXCEPTION 'Insufficient stock of % (have %, need %)', r.generic_name, r.stock_on_hand, r.quantity
                  USING ERRCODE = 'WC007';
        END IF;
        UPDATE medication SET stock_on_hand = stock_on_hand - r.quantity
         WHERE medication_id = r.medication_id;
        INSERT INTO stock_movement (medication_id, quantity, reason)
        VALUES (r.medication_id, -r.quantity, 'Dispensed prescription ' || p_prescription_id);
        v_lines := v_lines + 1;
    END LOOP;
    UPDATE prescription SET status = 'Dispensed' WHERE prescription_id = p_prescription_id;
    RETURN v_lines;
END $$;

\echo 'Portfolio 3 support objects created.'
