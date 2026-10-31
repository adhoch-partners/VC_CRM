"""Thin database layer so the same SQL runs on SQLite (local) and Postgres (hosted).

SQL is written once in SQLite style with `?` placeholders; the Postgres
connection rewrites placeholders and the schema is translated on startup.
Rows come back as mappings (`row["col"]`, `dict(row)`) on both backends.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def default_url():
    return os.environ.get("DATABASE_URL") or os.environ.get(
        "VC_CRM_DB", str(Path.cwd() / "data" / "vc_crm.db"))


def is_postgres(url):
    return url.startswith(("postgres://", "postgresql://"))


class IntegrityError(Exception):
    """kind: 'foreign_key', 'unique', or 'invalid' (check / not-null / bad value)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class Conn:
    def __init__(self, raw, postgres):
        self.raw = raw
        self.postgres = postgres

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql, params=()):
        with self._errors():
            return self.raw.execute(self._sql(sql), tuple(params))

    def executemany(self, sql, seq):
        with self._errors():
            if self.postgres:
                with self.raw.cursor() as cur:
                    cur.executemany(self._sql(sql), [tuple(p) for p in seq])
            else:
                self.raw.executemany(sql, [tuple(p) for p in seq])

    def insert(self, table, row):
        cols = ", ".join(row)
        marks = ", ".join("?" * len(row))
        return self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks}) RETURNING id",
                            list(row.values())).fetchone()["id"]

    @contextmanager
    def transaction(self):
        if self.postgres:
            with self._errors(), self.raw.transaction():
                yield self
        else:
            self.raw.execute("BEGIN")
            try:
                yield self
            except BaseException:
                self.raw.execute("ROLLBACK")
                raise
            self.raw.execute("COMMIT")

    @contextmanager
    def _errors(self):
        try:
            yield
        except sqlite3.IntegrityError as e:
            msg = str(e)
            kind = ("foreign_key" if "FOREIGN KEY" in msg
                    else "unique" if "UNIQUE" in msg else "invalid")
            raise IntegrityError(kind, msg) from e
        except Exception as e:
            if not self.postgres:
                raise
            from psycopg import errors as pg
            if isinstance(e, pg.ForeignKeyViolation):
                raise IntegrityError("foreign_key", str(e)) from e
            if isinstance(e, pg.UniqueViolation):
                raise IntegrityError("unique", str(e)) from e
            if isinstance(e, (pg.IntegrityError, pg.DataError)):
                raise IntegrityError("invalid", str(e)) from e
            raise

    def close(self):
        self.raw.close()


class Database:
    def __init__(self, url=None):
        self.url = url or default_url()
        self.postgres = is_postgres(self.url)

    def connect(self):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            raw = psycopg.connect(self.url, row_factory=dict_row, autocommit=True)
        else:
            # One connection per request, but FastAPI may open and close it on
            # different worker threads, so the same-thread check must be off.
            raw = sqlite3.connect(self.url, check_same_thread=False, isolation_level=None)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA foreign_keys = ON")
        return Conn(raw, self.postgres)

    def init(self):
        if not self.postgres:
            Path(self.url).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            script = SCHEMA.read_text()
            if self.postgres:
                script = to_postgres(script)
            for stmt in split_statements(script):
                conn.execute(stmt)
            for table, column, decl in ADDED_COLUMNS:
                if column not in columns(conn, table):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        finally:
            conn.close()


# Columns added after the first release, so existing databases pick them up.
ADDED_COLUMNS = [
    ("activities", "created_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("investments", "round_id", "INTEGER REFERENCES rounds(id) ON DELETE SET NULL"),
    ("audit_log", "record", "TEXT"),
    ("users", "totp_secret", "TEXT"),
    ("users", "totp_pending_secret", "TEXT"),
    ("users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "totp_last_counter", "INTEGER"),
    ("users", "email_reminders", "INTEGER NOT NULL DEFAULT 1"),
    # Custom field values (JSON) on every record type admins can add fields to.
    *[(t, "custom", "TEXT") for t in
      ("lps", "companies", "investors", "contacts", "commitments", "funds", "rounds")],
]


def columns(conn, table):
    if conn.postgres:
        rows = conn.execute("SELECT column_name AS name FROM information_schema.columns "
                            "WHERE table_name = ?", (table,))
    else:
        rows = conn.execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in rows}


def split_statements(script):
    script = re.sub(r"--[^\n]*", "", script)
    return [s.strip() for s in script.split(";") if s.strip()]


def to_postgres(script):
    script = re.sub(r"PRAGMA[^;]*;", "", script)
    script = script.replace("INTEGER PRIMARY KEY", "BIGSERIAL PRIMARY KEY")
    script = re.sub(r"\bREAL\b", "DOUBLE PRECISION", script)
    return script.replace(
        "(datetime('now'))", "(to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'))")
