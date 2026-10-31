"""Daily task reminder emails.

Once a day, after REMINDER_HOUR in REMINDER_TZ (default 7am America/New_York), each
person with open tasks that are overdue or due today gets one summary email, unless
they turned reminders off. A settings row records the last day sent, so a restart, or
a second copy of the app, does not send twice.
"""

import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from . import mailer

log = logging.getLogger("vc_crm.jobs")


def local_now():
    tz = ZoneInfo(os.environ.get("REMINDER_TZ", "America/New_York"))
    return datetime.now(tz)


def claim_day(conn, today):
    """True for exactly one caller per day."""
    with conn.transaction():
        row = conn.execute("SELECT value FROM settings WHERE key = 'digest_last_date'").fetchone()
        if row and row["value"] >= today:
            return False
        conn.execute("DELETE FROM settings WHERE key = 'digest_last_date'")
        conn.execute("INSERT INTO settings (key, value) VALUES ('digest_last_date', ?)", (today,))
    return True


def send_digests(conn, send_email, today):
    """Email each opted-in user their overdue and due-today tasks. Returns emails sent."""
    rows = conn.execute(
        "SELECT t.title, t.due_date, t.priority, u.id AS uid, u.name, u.email "
        "FROM tasks t JOIN users u ON u.id = t.assignee_id "
        "WHERE t.status = 'Open' AND t.due_date IS NOT NULL AND t.due_date <= ? "
        "AND u.active = 1 AND u.email_reminders = 1 ORDER BY u.id, t.due_date", (today,)).fetchall()
    by_user = {}
    for r in rows:
        by_user.setdefault(r["uid"], []).append(r)
    sent = 0
    for tasks in by_user.values():
        who = tasks[0]
        overdue = [t for t in tasks if t["due_date"] < today]
        due = [t for t in tasks if t["due_date"] == today]
        lines = []
        if overdue:
            lines += ["Overdue:"] + [f"  - {t['title']} (due {t['due_date']})"
                                     + (" [High]" if t["priority"] == "High" else "") for t in overdue]
        if due:
            lines += ["Due today:"] + [f"  - {t['title']}" + (" [High]" if t["priority"] == "High" else "")
                                       for t in due]
        body = (f"Hi {who['name']},\n\n" + "\n".join(lines) +
                f"\n\nOpen your tasks: {mailer.app_url()}/#tasks\n\n"
                "Turn these emails off under Security in the app's sidebar.")
        subject = f"VC CRM: {len(overdue)} overdue, {len(due)} due today"
        if send_email(who["email"], subject, body):
            sent += 1
    return sent


def start(db, send_email, interval_seconds=600):
    """Check every 10 minutes whether today's reminders are due; runs in the background."""
    hour = int(os.environ.get("REMINDER_HOUR", "7"))

    def loop():
        stop = threading.Event()
        while not stop.wait(interval_seconds):
            try:
                now = local_now()
                if now.hour < hour:
                    continue
                conn = db.connect()
                try:
                    today = now.date().isoformat()
                    if claim_day(conn, today):
                        log.info("Sent %d task reminder email(s)", send_digests(conn, send_email, today))
                finally:
                    conn.close()
            except Exception:  # never let the reminder loop die
                log.exception("Task reminder run failed")

    threading.Thread(target=loop, name="task-reminders", daemon=True).start()
