"""Two-factor sign-in, required 2FA, password reset by email, and API tokens."""

import re
import time

from fastapi.testclient import TestClient

from vc_crm import auth

from .conftest import PASSWORD, add_user, signed_in


def code_for(secret, offset=0):
    return auth._totp_at(secret, int(time.time() // auth.TOTP_STEP) + offset)


def enroll(c):
    setup = c.post("/api/auth/2fa/setup").json()
    assert setup["uri"].startswith("otpauth://totp/") and "<svg" in (setup["qr_svg"] or "<svg")
    r = c.post("/api/auth/2fa/enable", json={"code": code_for(setup["secret"])})
    assert r.status_code == 200, r.text
    return setup["secret"], r.json()["recovery_codes"]


def fresh(app):
    c = TestClient(app)
    c.headers["X-CRM"] = "1"
    return c


def test_two_factor_sign_in(app, database_url):
    c = signed_in(app, database_url, "a@firm.com", "editor")
    assert c.post("/api/auth/2fa/enable", json={"code": "123456"}).status_code == 400  # no setup
    secret, codes = enroll(c)
    assert len(codes) == 10 and c.get("/api/auth/me").json()["totp_enabled"] is True

    d = fresh(app)
    r = d.post("/api/auth/login", json={"email": "a@firm.com", "password": PASSWORD})
    assert r.json()["twofa_required"] is True and "set-cookie" not in r.headers
    assert d.get("/api/lps").status_code == 401  # no session until the code
    ch = r.json()["challenge"]
    assert d.post("/api/auth/2fa", json={"challenge": ch, "code": "000000"}).status_code == 401
    # The enable step used the current code; the next one is accepted (one step of drift).
    assert d.post("/api/auth/2fa", json={"challenge": ch, "code": code_for(secret, 1)}
                  ).status_code == 200
    assert d.get("/api/lps").status_code == 200

    # Same code again is a replay; a recovery code works exactly once.
    e = fresh(app)
    ch = e.post("/api/auth/login", json={"email": "a@firm.com", "password": PASSWORD}
                ).json()["challenge"]
    assert e.post("/api/auth/2fa", json={"challenge": ch, "code": code_for(secret, 1)}
                  ).status_code == 401
    assert e.post("/api/auth/2fa", json={"challenge": ch, "code": codes[0].upper()}
                  ).status_code == 200
    f = fresh(app)
    ch = f.post("/api/auth/login", json={"email": "a@firm.com", "password": PASSWORD}
                ).json()["challenge"]
    assert f.post("/api/auth/2fa", json={"challenge": ch, "code": codes[0]}).status_code == 401
    assert f.post("/api/auth/2fa", json={"challenge": "bogus", "code": codes[1]}
                  ).status_code == 401


def test_two_factor_attempts_are_throttled(app, database_url):
    c = signed_in(app, database_url, "t@firm.com", "viewer")
    enroll(c)
    d = fresh(app)
    ch = d.post("/api/auth/login", json={"email": "t@firm.com", "password": PASSWORD}
                ).json()["challenge"]
    for _ in range(10):
        d.post("/api/auth/2fa", json={"challenge": ch, "code": "000000"})
    assert d.post("/api/auth/2fa", json={"challenge": ch, "code": "111111"}).status_code == 429


def test_admin_can_require_two_factor(client, app, database_url):
    u = signed_in(app, database_url, "u@firm.com", "editor")
    assert client.put("/api/settings", json={"require_2fa": "1"}).status_code == 200
    # The admin has not enrolled either, so the admin is held at setup too.
    assert client.get("/api/lps").status_code == 403
    assert client.get("/api/auth/me").json()["must_enroll_2fa"] is True
    assert u.get("/api/lps").status_code == 403
    enroll(u)
    assert u.get("/api/lps").status_code == 200
    assert u.post("/api/auth/2fa/disable", json={"password": PASSWORD}).status_code == 400
    enroll(client)
    assert client.put("/api/settings", json={"require_2fa": "maybe"}).status_code == 400
    assert u.get("/api/settings").status_code == 403


def test_disable_and_admin_reset(client, app, database_url):
    u = signed_in(app, database_url, "u@firm.com", "editor")
    enroll(u)
    assert u.post("/api/auth/2fa/disable", json={"password": "wrong"}).status_code == 400
    assert u.post("/api/auth/2fa/recovery-codes", json={"password": PASSWORD}).status_code == 200
    uid = u.get("/api/auth/me").json()["id"]
    r = client.patch(f"/api/users/{uid}", json={"reset_2fa": True})
    assert r.status_code == 200 and r.json()["totp_enabled"] == 0
    assert not {"totp_secret", "password_hash"} & set(r.json())
    assert u.get("/api/auth/me").status_code == 401  # signed out
    d = fresh(app)
    assert "challenge" not in d.post("/api/auth/login", json={"email": "u@firm.com",
                                                             "password": PASSWORD}).json()
    log = client.get(f"/api/audit?entity=users&entity_id={uid}").json()
    assert log[0]["changes"] == {"two_factor": "reset"}


def test_password_reset_by_email(app, database_url, outbox):
    add_user(database_url, "r@firm.com", "editor")
    old = signed_in(app, database_url, "r2@firm.com", "viewer")  # unrelated session
    session = fresh(app)
    session.post("/api/auth/login", json={"email": "r@firm.com", "password": PASSWORD})
    anon = fresh(app)
    assert anon.post("/api/auth/forgot", json={"email": "nobody@firm.com"}).status_code == 204
    assert outbox == []  # unknown address: same answer, no email
    assert anon.post("/api/auth/forgot", json={"email": "R@Firm.com"}).status_code == 204
    (to, subject, body), = outbox
    assert to == "r@firm.com" and "#reset=" in body
    token = re.search(r"#reset=(\S+)", body).group(1)
    assert anon.post("/api/auth/reset", json={"token": token, "new_password": "short"}
                     ).status_code == 400
    assert anon.post("/api/auth/reset", json={"token": token,
                                              "new_password": "a whole new password"}
                     ).status_code == 204
    assert anon.post("/api/auth/reset", json={"token": token,
                                              "new_password": "and once more please"}
                     ).status_code == 400  # single use
    assert session.get("/api/auth/me").status_code == 401  # signed out everywhere
    assert old.get("/api/auth/me").status_code == 200
    assert anon.post("/api/auth/login", json={"email": "r@firm.com",
                                              "password": "a whole new password"}
                     ).status_code == 200


def test_api_tokens(client, app, database_url):
    lp = client.post("/api/lps", json={"name": "LP"}).json()
    r = client.post("/api/tokens", json={"name": "Claude"})
    assert r.status_code == 201
    token = r.json()["token"]
    assert token.startswith("crm_")
    listed = client.get("/api/tokens").json()
    assert listed[0]["name"] == "Claude" and "token" not in listed[0]

    bot = TestClient(app)  # no cookie, no X-CRM header
    bot.headers["Authorization"] = f"Bearer {token}"
    assert bot.get("/api/lps").json()[0]["name"] == "LP"
    act = bot.post("/api/activities", json={"entity": "lps", "entity_id": lp["id"],
                                            "activity_date": "2026-10-30", "kind": "Meeting",
                                            "summary": "From Granola"})
    assert act.status_code == 201
    assert bot.post("/api/tokens", json={"name": "x"}).status_code == 403
    assert client.get("/api/tokens").json()[0]["last_used_at"] is not None

    client.delete(f"/api/tokens/{listed[0]['id']}")
    assert bot.get("/api/lps").status_code == 401
    bad = TestClient(app)
    bad.headers["Authorization"] = "Bearer crm_nope"
    assert bad.get("/api/lps").status_code == 401


def test_viewer_token_is_read_only(app, database_url):
    v = signed_in(app, database_url, "v@firm.com", "viewer")
    token = v.post("/api/tokens", json={"name": "read"}).json()["token"]
    bot = TestClient(app)
    bot.headers["Authorization"] = f"Bearer {token}"
    assert bot.get("/api/investors").status_code == 200
    assert bot.post("/api/lps", json={"name": "x"}).status_code == 403
