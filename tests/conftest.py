"""Every API test runs on SQLite, and also on Postgres when TEST_DATABASE_URL is set.

TEST_DATABASE_URL must point at a throwaway database: its public schema is
dropped and recreated before each test.
"""

import os

import pytest
from fastapi.testclient import TestClient

from vc_crm import auth
from vc_crm.app import create_app
from vc_crm.db import Database

PG_URL = os.environ.get("TEST_DATABASE_URL")
PASSWORD = "correct horse battery"


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request, tmp_path):
    if request.param == "sqlite":
        return str(tmp_path / "test.db")
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
    return PG_URL


@pytest.fixture
def outbox():
    """Emails the app 'sent' during the test: (to, subject, body)."""
    return []


@pytest.fixture
def app(database_url, outbox):
    return create_app(database_url, cookie_secure=False,
                      send_email=lambda to, subject, body: outbox.append((to, subject, body)) or True)


def add_user(database_url, email, role, password=PASSWORD):
    conn = Database(database_url).connect()
    try:
        return conn.insert("users", {"email": email, "name": email.split("@")[0].title(),
                                     "role": role, "password_hash": auth.hash_password(password)})
    finally:
        conn.close()


def signed_in(app, database_url, email, role):
    add_user(database_url, email, role)
    c = TestClient(app)
    c.headers["X-CRM"] = "1"
    r = c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def client(app, database_url):
    """Signed in as an admin."""
    return signed_in(app, database_url, "admin@firm.com", "admin")
