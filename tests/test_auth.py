import pytest
from fastapi.testclient import TestClient

from vc_crm import auth
from vc_crm.app import create_app
from vc_crm.db import Database

from .conftest import PASSWORD, add_user, signed_in


@pytest.fixture
def anon(app):
    c = TestClient(app)
    c.headers["X-CRM"] = "1"
    return c


@pytest.fixture
def viewer(app, database_url):
    return signed_in(app, database_url, "viewer@firm.com", "viewer")


@pytest.fixture
def editor(app, database_url):
    return signed_in(app, database_url, "editor@firm.com", "editor")


def test_password_hashing():
    h = auth.hash_password("s3cret-password")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert auth.verify_password("s3cret-password", h)
    assert not auth.verify_password("wrong-password", h)
    assert not auth.verify_password("x", "garbage")


def test_everything_but_login_requires_a_session(anon):
    for path in ["/api/meta", "/api/lps", "/api/dashboard", "/api/portfolio", "/api/audit",
                 "/api/auth/me", "/api/users/directory"]:
        assert anon.get(path).status_code == 401, path
    assert anon.post("/api/lps", json={"name": "x"}).status_code == 401
    assert anon.get("/healthz").json() == {"ok": True}
    assert anon.get("/").status_code == 200


def test_login_logout(anon, database_url):
    add_user(database_url, "a@firm.com", "editor")
    assert anon.post("/api/auth/login", json={"email": "a@firm.com", "password": "nope"}
                     ).status_code == 401
    r = anon.post("/api/auth/login", json={"email": " A@Firm.com ", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["role"] == "editor"
    assert "password_hash" not in r.json()
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert anon.get("/api/auth/me").json()["email"] == "a@firm.com"
    anon.post("/api/auth/logout")
    assert anon.get("/api/auth/me").status_code == 401


def test_session_token_is_stored_hashed(client, database_url):
    token = client.cookies.get(auth.COOKIE)
    conn = Database(database_url).connect()
    stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM sessions")]
    conn.close()
    assert token not in stored and auth.token_hash(token) in stored


def test_expired_session_rejected(client, database_url):
    conn = Database(database_url).connect()
    conn.execute("UPDATE sessions SET expires_at = '2000-01-01 00:00:00'")
    conn.close()
    assert client.get("/api/auth/me").status_code == 401


def test_writes_need_csrf_header(client):
    del client.headers["X-CRM"]
    assert client.post("/api/lps", json={"name": "x"}).status_code == 403
    assert client.get("/api/lps").status_code == 200


def test_login_throttled_after_repeated_failures(anon, database_url):
    add_user(database_url, "t@firm.com", "viewer")
    for _ in range(10):
        anon.post("/api/auth/login", json={"email": "t@firm.com", "password": "wrong"})
    r = anon.post("/api/auth/login", json={"email": "t@firm.com", "password": PASSWORD})
    assert r.status_code == 429


def test_viewer_is_read_only(client, viewer):
    lp = client.post("/api/lps", json={"name": "LP"}).json()
    assert viewer.get("/api/lps").status_code == 200
    assert viewer.get("/api/dashboard").status_code == 200
    assert viewer.post("/api/lps", json={"name": "x"}).status_code == 403
    assert viewer.patch(f"/api/lps/{lp['id']}", json={"name": "y"}).status_code == 403
    assert viewer.delete(f"/api/lps/{lp['id']}").status_code == 403
    assert viewer.post("/api/capital-events", json={}).status_code == 403
    assert viewer.patch("/api/allocations/1", json={"paid": True}).status_code == 403
    assert viewer.get("/api/users").status_code == 403


def test_editor_edits_but_cannot_manage_users(editor):
    assert editor.post("/api/lps", json={"name": "LP"}).status_code == 201
    assert editor.get("/api/users").status_code == 403
    assert editor.post("/api/users", json={}).status_code == 403
    assert editor.get("/api/users/directory").status_code == 200


def test_admin_user_management(client, app):
    r = client.post("/api/users", json={"email": "New@Firm.com", "name": "New Person",
                                        "role": "editor", "password": "short"})
    assert r.status_code == 400  # password policy
    r = client.post("/api/users", json={"email": "New@Firm.com", "name": "New Person",
                                        "role": "editor", "password": PASSWORD})
    assert r.status_code == 201
    u = r.json()
    assert u["email"] == "new@firm.com" and "password_hash" not in u
    assert client.post("/api/users", json={"email": "new@firm.com", "name": "Dup",
                                           "role": "viewer", "password": PASSWORD}
                       ).status_code == 409
    assert all("password_hash" not in x for x in client.get("/api/users").json())

    other = TestClient(app)
    other.headers["X-CRM"] = "1"
    assert other.post("/api/auth/login", json={"email": "new@firm.com", "password": PASSWORD}
                      ).status_code == 200
    # Role change applies on the next request, no re-login needed.
    client.patch(f"/api/users/{u['id']}", json={"role": "viewer"})
    assert other.post("/api/lps", json={"name": "x"}).status_code == 403
    # Deactivation signs the user out and blocks sign-in.
    client.patch(f"/api/users/{u['id']}", json={"active": False})
    assert other.get("/api/auth/me").status_code == 401
    assert other.post("/api/auth/login", json={"email": "new@firm.com", "password": PASSWORD}
                      ).status_code == 401


def test_cannot_remove_last_admin(client):
    me = client.get("/api/auth/me").json()
    assert client.patch(f"/api/users/{me['id']}", json={"role": "editor"}).status_code == 400
    assert client.patch(f"/api/users/{me['id']}", json={"active": False}).status_code == 400
    second = client.post("/api/users", json={"email": "b@firm.com", "name": "B", "role": "admin",
                                             "password": PASSWORD}).json()
    assert client.patch(f"/api/users/{second['id']}", json={"active": False}).status_code == 200


def test_admin_password_reset_signs_user_out(client, app, viewer):
    uid = viewer.get("/api/auth/me").json()["id"]
    assert client.patch(f"/api/users/{uid}", json={"password": "a brand new password"}
                        ).status_code == 200
    assert viewer.get("/api/auth/me").status_code == 401


def test_change_own_password(client, app, database_url):
    assert client.post("/api/auth/password", json={"current_password": "wrong",
                                                   "new_password": "another long one"}
                       ).status_code == 400
    # A second device is signed out; this one stays signed in.
    other = TestClient(app)
    other.headers["X-CRM"] = "1"
    other.post("/api/auth/login", json={"email": "admin@firm.com", "password": PASSWORD})
    assert client.post("/api/auth/password", json={"current_password": PASSWORD,
                                                   "new_password": "another long one"}
                       ).status_code == 204
    assert client.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    assert other.post("/api/auth/login", json={"email": "admin@firm.com", "password": PASSWORD}
                      ).status_code == 401


def test_audit_log_and_activity_author(client):
    me = client.get("/api/auth/me").json()
    lp = client.post("/api/lps", json={"name": "LP"}).json()
    client.patch(f"/api/lps/{lp['id']}", json={"location": "NYC"})
    client.patch(f"/api/lps/{lp['id']}", json={"location": "NYC"})  # no-op: not logged
    log = client.get(f"/api/audit?entity=lps&entity_id={lp['id']}").json()
    assert [e["action"] for e in log] == ["update", "create"]
    assert log[0]["changes"] == {"location": {"from": None, "to": "NYC"}}
    assert log[0]["user_name"] == "Admin"

    act = client.post("/api/activities", json={"entity": "lps", "entity_id": lp["id"],
                                               "activity_date": "2026-01-05", "summary": "Call"})
    assert act.json()["created_by"] == me["id"]
    assert client.post("/api/activities", json={"entity": "lps", "entity_id": lp["id"],
                                                "activity_date": "2026-01-05", "summary": "x",
                                                "created_by": 999}).status_code == 400


def test_bootstrap_admin_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "Boss@Firm.com")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    c = TestClient(create_app(str(tmp_path / "b.db"), cookie_secure=False))
    c.headers["X-CRM"] = "1"
    assert c.post("/api/auth/login", json={"email": "boss@firm.com", "password": PASSWORD}
                  ).json()["role"] == "admin"
    # Only when there are no users: a restart with a different password changes nothing.
    monkeypatch.setenv("ADMIN_PASSWORD", "something else entirely")
    create_app(str(tmp_path / "b.db"), cookie_secure=False)
    assert c.post("/api/auth/login", json={"email": "boss@firm.com", "password": PASSWORD}
                  ).status_code == 200


def test_security_headers(anon):
    r = anon.get("/")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert anon.get("/api/lps").headers["cache-control"] == "no-store"
    assert anon.get("/docs").status_code == 404


def test_throttle_forgets_stale_keys():
    t = auth.LoginThrottle(max_failures=2, window_seconds=0)
    for i in range(10_005):
        t.fail(f"email:{i}")
    assert len(t.failures) < 10_005


def test_upgrade_from_single_user_database(tmp_path):
    """A database created before logins existed gains the new tables and columns."""
    import sqlite3

    path = str(tmp_path / "v1.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE lps (id INTEGER PRIMARY KEY, name TEXT NOT NULL, lp_type TEXT,
            contact_name TEXT, email TEXT, phone TEXT, location TEXT, relationship_owner TEXT,
            tags TEXT, notes TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE activities (id INTEGER PRIMARY KEY, entity TEXT NOT NULL,
            entity_id INTEGER NOT NULL, activity_date TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'Note', summary TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO lps (name) VALUES ('Existing LP');
        INSERT INTO activities (entity, entity_id, activity_date, summary)
            VALUES ('lps', 1, '2026-01-01', 'Old note');
    """)
    old.close()
    c = TestClient(create_app(path, cookie_secure=False))
    add_user(path, "a@firm.com", "admin")
    c.headers["X-CRM"] = "1"
    c.post("/api/auth/login", json={"email": "a@firm.com", "password": PASSWORD})
    assert [x["name"] for x in c.get("/api/lps").json()] == ["Existing LP"]
    acts = c.get("/api/activities").json()
    assert acts[0]["summary"] == "Old note" and acts[0]["created_by"] is None
