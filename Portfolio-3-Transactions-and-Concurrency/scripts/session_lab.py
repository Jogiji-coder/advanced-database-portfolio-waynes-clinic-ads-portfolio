"""
session_lab.py - drive several real PostgreSQL sessions step by step.

Each Session is its own connection (its own backend process). Statements are
sent explicitly (BEGIN / COMMIT / ROLLBACK included), so the interleaving is
exactly the one written in the demo. A statement that is expected to block is
run with .start() in a background thread; the monitor session can then inspect
pg_locks / pg_stat_activity while it waits.
"""
import os
import textwrap
import threading
import time

import psycopg
from PIL import Image, ImageDraw, ImageFont

DSN = os.environ.get("WC_DSN", "host=localhost dbname=waynes_clinic user=postgres password=postgres")
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


class Log:
    def __init__(self, title, subtitle=""):
        self.title, self.subtitle = title, subtitle
        self.t0 = time.perf_counter()
        self.events = []            # (ms, who, kind, text)
        self.lock = threading.Lock()

    def add(self, who, kind, text):
        with self.lock:
            self.events.append(((time.perf_counter() - self.t0) * 1000, who, kind, text))

    def note(self, text):
        self.add("NOTE", "note", text)

    def verdict(self, ok, text):
        self.add("RESULT", "good" if ok else "bad", text)

    def section(self, text):
        self.add("SECTION", "section", text)

    # ---------------------------------------------------------------- text log
    def to_text(self):
        out = [f"# {self.title}", f"# {self.subtitle}" if self.subtitle else "", ""]
        for ms, who, kind, text in self.events:
            if kind == "section":
                out += ["", f"=== {text} " + "=" * max(0, 70 - len(text))]
                continue
            tag = {"sql": "", "out": "   -> ", "err": "   !! ", "wait": "   .. ", "note": "# ",
                   "good": "", "bad": "", "mon": "   "}[kind]
            for i, line in enumerate(text.split("\n")):
                prefix = f"{ms:8.0f} ms  [{who}] " if i == 0 else " " * (14 + len(who) + 3)
                out.append(prefix + (tag if i == 0 else " " * len(tag)) + line)
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------ timeline PNG
    def to_png(self, path, names=("A", "B")):
        f = ImageFont.truetype(MONO, 14)
        fb = ImageFont.truetype(MONO_B, 14)
        ft = ImageFont.truetype(MONO_B, 18)
        fs = ImageFont.truetype(MONO, 13)
        cw = f.getbbox("M")[2]
        lh = 20
        col_chars = 66
        tcol, colw = 92, cw * col_chars + 24
        W = tcol + 2 * colw + 20
        colours = {"sql": "#e2e8f0", "out": "#93c5fd", "err": "#f87171", "wait": "#fbbf24",
                   "note": "#94a3b8", "good": "#4ade80", "bad": "#f87171", "mon": "#c4b5fd", "section": "#facc15"}
        rows = []
        for ms, who, kind, text in self.events:
            if kind in ("note", "good", "bad", "section", "mon"):
                wrapped = sum((textwrap.wrap(t, 2 * col_chars - 6, subsequent_indent="    ") or [""] for t in text.split("\n")), [])
                rows.append((ms, who, kind, wrapped, "span"))
            else:
                if who not in names:              # a third session is drawn in column B, labelled
                    text = f"[{who}] " + text
                wrapped = sum((textwrap.wrap(t, col_chars, subsequent_indent="  ") or [""] for t in text.split("\n")), [])
                rows.append((ms, who, kind, wrapped, who))
        H = 110 + sum(len(r[3]) * lh + 8 for r in rows) + 40
        img = Image.new("RGB", (W, H), "#0f172a")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 34], fill="#1e293b")
        for i, c in enumerate(["#ef4444", "#f59e0b", "#22c55e"]):
            d.ellipse([14 + i * 22, 11, 26 + i * 22, 23], fill=c)
        d.text((90, 7), self.title, font=ft, fill="#f8fafc")
        d.text((20, 44), self.subtitle, font=fs, fill="#94a3b8")
        y0 = 72
        d.text((12, y0), "time", font=fb, fill="#64748b")
        d.text((tcol + 10, y0), f"SESSION {names[0]}", font=fb, fill="#67e8f9")
        d.text((tcol + colw + 10, y0), f"SESSION {names[1]}", font=fb, fill="#f9a8d4")
        y = y0 + 28
        d.line([tcol, y0, tcol, H - 30], fill="#334155")
        d.line([tcol + colw, y0, tcol + colw, H - 30], fill="#334155")
        for ms, who, kind, lines, col in rows:
            x = 20 if col == "span" else (tcol + 10 if col == names[0] else tcol + colw + 10)
            if col == "span" and kind != "section":
                x = tcol + 10
            d.text((10, y), f"{ms:6.0f}ms", font=fs, fill="#475569")
            for i, line in enumerate(lines):
                fnt = fb if kind in ("good", "bad", "section", "err") else f
                d.text((x, y + i * lh), line, font=fnt, fill=colours[kind])
            y += len(lines) * lh + 8
        d.text((20, H - 26), "Rendered from the timestamped session log in the matching .txt file "
                             "(each session is a separate PostgreSQL backend).", font=fs, fill="#475569")
        img.save(path, optimize=True)


class Session:
    def __init__(self, name, log, app=None):
        self.name, self.log = name, log
        self.conn = psycopg.connect(DSN, autocommit=True, application_name=app or f"session_{name}")
        for s in ["SET search_path TO clinic, public", "SET timezone TO 'America/New_York'",
                  "SET statement_timeout = '20s'"]:
            self.conn.execute(s)
        self.pid = self.conn.info.backend_pid
        self.thread = None
        self.result = None

    def _fmt(self, cur):
        if cur.description is None:
            return cur.statusmessage
        rows = cur.fetchall()
        cols = [c.name for c in cur.description]
        if len(rows) == 1 and len(cols) == 1:
            return f"{cols[0]} = {rows[0][0]}"
        if len(rows) <= 6:
            return "\n".join(", ".join(f"{c}={v}" for c, v in zip(cols, r)) for r in rows) or "(0 rows)"
        return f"({len(rows)} rows)"

    def run(self, sql, params=None, quiet=False, label=None):
        if not quiet:
            self.log.add(self.name, "sql", label or " ".join(sql.split()))
        t0 = time.perf_counter()
        try:
            cur = self.conn.execute(sql, params)
            txt = self._fmt(cur)
            waited = (time.perf_counter() - t0) * 1000
            echo = txt == sql.split()[0].upper()          # e.g. BEGIN -> BEGIN adds nothing
            if not quiet and not (echo and waited < 250):
                self.log.add(self.name, "out", txt + (f"   (waited {waited:,.0f} ms)" if waited > 250 else ""))
            self.result = txt
            return txt
        except psycopg.Error as e:
            msg = f"ERROR {e.sqlstate}: {e.diag.message_primary}"
            self.log.add(self.name, "err", msg)
            self.result = e
            return e

    def value(self, sql, params=None):
        """Silent helper: fetch a single value without logging."""
        return self.conn.execute(sql, params).fetchone()[0]

    def start(self, sql, params=None, label=None):
        """Send a statement that is expected to block; returns immediately."""
        self.log.add(self.name, "sql", label or " ".join(sql.split()))
        self.result = None

        def target():
            t0 = time.perf_counter()
            try:
                cur = self.conn.execute(sql, params)
                txt = self._fmt(cur)
                self.log.add(self.name, "out", f"{txt}   (unblocked after {(time.perf_counter() - t0) * 1000:,.0f} ms)")
                self.result = txt
            except psycopg.Error as e:
                self.log.add(self.name, "err", f"ERROR {e.sqlstate}: {e.diag.message_primary}"
                             f"   (after {(time.perf_counter() - t0) * 1000:,.0f} ms)")
                self.result = e
        self.thread = threading.Thread(target=target)
        self.thread.start()
        time.sleep(0.4)
        if self.thread.is_alive():
            self.log.add(self.name, "wait", "... BLOCKED - waiting for a lock held by another session")

    def wait(self, timeout=25):
        if self.thread:
            self.thread.join(timeout)
            self.thread = None
        return self.result

    def close(self):
        try:
            self.conn.execute("ROLLBACK")
        except Exception:
            pass
        self.conn.close()


class Monitor(Session):
    def snapshot(self, *pids):
        rows = self.conn.execute("""
            SELECT a.application_name, a.pid, a.state, coalesce(a.wait_event_type || '/' || a.wait_event, '-') AS waiting_on,
                   pg_blocking_pids(a.pid) AS blocked_by,
                   left(regexp_replace(a.query, '\\s+', ' ', 'g'), 60) AS query
            FROM pg_stat_activity a WHERE a.pid = ANY(%s) ORDER BY a.application_name""", (list(pids),)).fetchall()
        names = {}
        for r in rows:
            names[r[1]] = r[0].replace("session_", "")
        lines = []
        for app, pid, state, wait, blocked, q in rows:
            who = app.replace("session_", "")
            by = ", ".join(names.get(b, str(b)) for b in blocked) or "-"
            lines.append(f"{who}: pid {pid}, {state}, wait={wait}, blocked_by={by} | {q}")
        locks = self.conn.execute("""
            SELECT a.application_name, l.locktype, l.mode, l.granted,
                   coalesce(l.relation::regclass::text, l.transactionid::text, '') AS target
            FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.pid = ANY(%s) AND (NOT l.granted OR l.locktype IN ('transactionid'))
            ORDER BY l.granted, a.application_name""", (list(pids),)).fetchall()
        for app, lt, mode, granted, target in locks:
            lines.append(f"  lock {'GRANTED' if granted else 'WAITING'}: {app.replace('session_', '')} "
                         f"{mode} on {lt} {target}")
        self.log.add("MONITOR", "mon", "pg_stat_activity + pg_locks:\n" + "\n".join(lines))
