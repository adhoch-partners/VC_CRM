"""Two-factor sign-in, password reset by email, app settings, and personal API tokens."""

import io
from datetime import timedelta

from fastapi import Body, Depends, HTTPException, Request, Response

from . import auth, mailer

CHALLENGE_MINUTES = 5
RESET_MINUTES = 60
SETTINGS = {"require_2fa": ("0", "1")}  # key: allowed values


def new_challenge(conn, user_id):
    token = auth.secrets.token_urlsafe(32)
    with conn.transaction():
        conn.execute("DELETE FROM login_challenges WHERE expires_at < ?",
                     (auth.iso(auth.utcnow()),))
        conn.execute("INSERT INTO login_challenges (token_hash, user_id, expires_at) "
                     "VALUES (?, ?, ?)",
                     (auth.token_hash(token), user_id,
                      auth.iso(auth.utcnow() + timedelta(minutes=CHALLENGE_MINUTES))))
    return token


def qr_svg(text):
    try:
        import segno
    except ImportError:  # the setup key can still be typed in by hand
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, border=2, xmldecl=False)
    return buf.getvalue().decode()


def store_recovery_codes(conn, user_id):
    codes = auth.new_recovery_codes()
    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    conn.executemany("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
                     [(user_id, auth.token_hash(auth.normalize_recovery_code(c))) for c in codes])
    return codes


def check_second_factor(conn, user, code):
    """True if `code` is a current authenticator code or an unused recovery code."""
    step = auth.verify_totp(user["totp_secret"], code, user["totp_last_counter"])
    if step is not None:
        conn.execute("UPDATE users SET totp_last_counter = ? WHERE id = ?", (step, user["id"]))
        return True
    norm = auth.normalize_recovery_code(code)
    if len(norm) != 8:
        return False
    row = conn.execute("SELECT id FROM recovery_codes WHERE user_id = ? AND code_hash = ? "
                       "AND used_at IS NULL", (user["id"], auth.token_hash(norm))).fetchone()
    if not row:
        return False
    conn.execute("UPDATE recovery_codes SET used_at = ? WHERE id = ?",
                 (auth.iso(auth.utcnow()), row["id"]))
    return True


def register(app, d):
    get_conn, viewer, admin = d.get_conn, d.viewer, d.admin

    def full_user(conn, uid):
        return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())

    # ------------------------------------------------------------ two-factor sign-in

    @app.post("/api/auth/2fa")
    def second_factor(request: Request, response: Response, data: dict = Body(...),
                      conn=Depends(get_conn)):
        d.check_csrf(request)
        row = conn.execute(
            "SELECT user_id FROM login_challenges WHERE token_hash = ? AND expires_at > ?",
            (auth.token_hash(str(data.get("challenge", ""))), auth.iso(auth.utcnow()))).fetchone()
        if not row:
            raise HTTPException(401, "Sign-in expired. Enter your email and password again.")
        key = f"2fa:{row['user_id']}"
        if d.throttle.blocked(key):
            raise HTTPException(429, "Too many wrong codes. Try again in 15 minutes.")
        user = full_user(conn, row["user_id"])
        if not user["active"] or not check_second_factor(conn, user, data.get("code")):
            d.throttle.fail(key)
            raise HTTPException(401, "That code is not valid. Check your authenticator app.")
        d.throttle.clear(key)
        conn.execute("DELETE FROM login_challenges WHERE user_id = ?", (user["id"],))
        return d.start_session(response, conn, user)

    # ------------------------------------------------------------ two-factor setup

    @app.post("/api/auth/2fa/setup")
    def setup_2fa(user=Depends(viewer), conn=Depends(get_conn)):
        if user["totp_enabled"]:
            raise HTTPException(400, "Two-factor is already on. Turn it off first to re-link.")
        secret = auth.new_totp_secret()
        conn.execute("UPDATE users SET totp_pending_secret = ? WHERE id = ?", (secret, user["id"]))
        uri = auth.totp_uri(secret, user["email"])
        return {"secret": secret, "uri": uri, "qr_svg": qr_svg(uri)}

    @app.post("/api/auth/2fa/enable")
    def enable_2fa(data: dict = Body(...), user=Depends(viewer), conn=Depends(get_conn)):
        u = full_user(conn, user["id"])
        step = auth.verify_totp(u["totp_pending_secret"], data.get("code"))
        if step is None:
            raise HTTPException(400, "That code does not match. Make sure your phone's clock is "
                                     "set automatically and try the newest code.")
        with conn.transaction():
            conn.execute("UPDATE users SET totp_secret = totp_pending_secret, "
                         "totp_pending_secret = NULL, totp_enabled = 1, totp_last_counter = ? "
                         "WHERE id = ?", (step, user["id"]))
            codes = store_recovery_codes(conn, user["id"])
            d.audit(conn, user, "update", "users", user["id"], {"two_factor": "turned on"})
        return {"recovery_codes": codes}

    def confirm_password(conn, user, password):
        u = full_user(conn, user["id"])
        if not auth.verify_password(str(password or ""), u["password_hash"]):
            raise HTTPException(400, "Password is incorrect")
        return u

    @app.post("/api/auth/2fa/disable", status_code=204)
    def disable_2fa(data: dict = Body(...), user=Depends(viewer), conn=Depends(get_conn)):
        from .app import require_2fa
        if require_2fa(conn):
            raise HTTPException(400, "Your admin requires two-factor for everyone.")
        confirm_password(conn, user, data.get("password"))
        with conn.transaction():
            conn.execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL, "
                         "totp_last_counter = NULL WHERE id = ?", (user["id"],))
            conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user["id"],))
            d.audit(conn, user, "update", "users", user["id"], {"two_factor": "turned off"})

    @app.post("/api/auth/2fa/recovery-codes")
    def new_recovery_codes(data: dict = Body(...), user=Depends(viewer), conn=Depends(get_conn)):
        u = confirm_password(conn, user, data.get("password"))
        if not u["totp_enabled"]:
            raise HTTPException(400, "Turn on two-factor first")
        with conn.transaction():
            codes = store_recovery_codes(conn, user["id"])
            d.audit(conn, user, "update", "users", user["id"],
                    {"two_factor": "new recovery codes"})
        return {"recovery_codes": codes}

    # ------------------------------------------------------------ password reset by email

    @app.post("/api/auth/forgot", status_code=204)
    def forgot_password(request: Request, data: dict = Body(...), conn=Depends(get_conn)):
        d.check_csrf(request)
        email = str(data.get("email", "")).strip().lower()
        keys = (f"forgot:{email}", f"forgot-ip:{request.client.host if request.client else '?'}")
        if d.throttle.blocked(*keys):
            return  # same silent answer either way
        d.throttle.fail(*keys)  # counts requests, not failures: at most 10 per 15 minutes
        user = conn.execute("SELECT id, name, email, active FROM users WHERE email = ?",
                            (email,)).fetchone()
        if not user or not user["active"]:
            return  # never reveal whether an account exists
        token = auth.secrets.token_urlsafe(32)
        conn.execute("INSERT INTO password_resets (token_hash, user_id, expires_at) "
                     "VALUES (?, ?, ?)",
                     (auth.token_hash(token), user["id"],
                      auth.iso(auth.utcnow() + timedelta(minutes=RESET_MINUTES))))
        d.send_email(user["email"], "Reset your VC CRM password", (
            f"Hi {user['name']},\n\nSomeone asked to reset your VC CRM password. If it was "
            f"you, open this link within {RESET_MINUTES} minutes:\n\n"
            f"{mailer.app_url()}/#reset={token}\n\nIf it was not you, ignore this email; "
            "your password has not changed."))

    @app.post("/api/auth/reset", status_code=204)
    def reset_password(request: Request, data: dict = Body(...), conn=Depends(get_conn)):
        d.check_csrf(request)
        row = conn.execute(
            "SELECT user_id FROM password_resets WHERE token_hash = ? AND used_at IS NULL "
            "AND expires_at > ?",
            (auth.token_hash(str(data.get("token", ""))), auth.iso(auth.utcnow()))).fetchone()
        if not row:
            raise HTTPException(400, "This reset link has expired or was already used. "
                                     "Ask for a new one.")
        if problem := auth.check_password_policy(data.get("new_password")):
            raise HTTPException(400, problem)
        uid = row["user_id"]
        with conn.transaction():
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (auth.hash_password(data["new_password"]), uid))
            conn.execute("UPDATE password_resets SET used_at = ? WHERE user_id = ? "
                         "AND used_at IS NULL", (auth.iso(auth.utcnow()), uid))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            d.audit(conn, {"id": uid}, "update", "users", uid, {"password": "reset by email"})

    # ------------------------------------------------------------ admin settings

    @app.get("/api/settings")
    def get_settings(user=Depends(admin), conn=Depends(get_conn)):
        from .app import get_setting
        return {k: get_setting(conn, k, allowed[0]) for k, allowed in SETTINGS.items()}

    @app.put("/api/settings")
    def put_settings(data: dict = Body(...), user=Depends(admin), conn=Depends(get_conn)):
        for k, v in data.items():
            if k not in SETTINGS or str(v) not in SETTINGS[k]:
                raise HTTPException(400, f"Invalid setting {k}={v!r}")
        with conn.transaction():
            for k, v in data.items():
                conn.execute("DELETE FROM settings WHERE key = ?", (k,))
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
            d.audit(conn, user, "update", "settings", None, {k: str(v) for k, v in data.items()})
        return get_settings(user, conn)

    # ------------------------------------------------------------ API tokens

    @app.get("/api/tokens")
    def list_tokens(user=Depends(viewer), conn=Depends(get_conn)):
        return [dict(r) for r in conn.execute(
            "SELECT id, name, prefix, last_used_at, created_at FROM api_tokens "
            "WHERE user_id = ? ORDER BY id DESC", (user["id"],))]

    @app.post("/api/tokens", status_code=201)
    def create_token(request: Request, data: dict = Body(...), user=Depends(viewer),
                     conn=Depends(get_conn)):
        if request.headers.get("authorization"):
            raise HTTPException(403, "Create tokens from the app, not with another token")
        name = str(data.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "Give the token a name, e.g. 'Claude meeting notes'")
        token = auth.new_api_token()
        with conn.transaction():
            tid = conn.insert("api_tokens", {"user_id": user["id"], "name": name,
                                             "token_hash": auth.token_hash(token),
                                             "prefix": token[:10]})
            d.audit(conn, user, "update", "users", user["id"], {"api_token": f"created '{name}'"})
        return {"id": tid, "name": name, "token": token}

    @app.delete("/api/tokens/{tid}", status_code=204)
    def revoke_token(tid: int, user=Depends(viewer), conn=Depends(get_conn)):
        row = conn.execute("SELECT name FROM api_tokens WHERE id = ? AND user_id = ?",
                           (tid, user["id"])).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        with conn.transaction():
            conn.execute("DELETE FROM api_tokens WHERE id = ?", (tid,))
            d.audit(conn, user, "update", "users", user["id"],
                    {"api_token": f"revoked '{row['name']}'"})
