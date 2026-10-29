import os
import sqlite3
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_DB = os.environ.get("VC_CRM_DB", str(Path.cwd() / "data" / "vc_crm.db"))


def connect(path):
    # One connection per request, but FastAPI may open and close it on
    # different worker threads, so the same-thread check must be off.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA.read_text())
