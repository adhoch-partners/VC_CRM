import json
import os
import re
from datetime import date, timedelta
from types import SimpleNamespace
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import auth, jobs, mailer, metrics
from . import data_io, search, security
from .access import LEVELS, PARENTS, Access, deny
from .db import Database, IntegrityError
from .entities import CAPITAL_EVENT_FIELDS, DEAL_STAGES, ENTITIES, LP_STAGES

STATIC = Path(__file__).with_name("static")


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; frame-ancestors 'none'"),
}


def env_flag(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def coerce(fields, data, partial):
    by_name = {fl["name"]: fl for fl in fields}
    unknown = set(data) - set(by_name)
    if unknown:
        raise HTTPException(400, f"Unknown field(s): {', '.join(sorted(unknown))}")
    out = {}
    for name, value in data.items():
        fl = by_name[name]
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None):
            if fl.get("required"):
                raise HTTPException(400, f"{fl['label']} is required")
            out[name] = None
            continue
        t = fl["type"]
        try:
            if t in ("money", "number", "percent"):
                value = float(str(value).replace(",", "").replace("$", "").replace("%", ""))
            elif t in ("integer", "ref"):
                value = int(value)
            elif t == "date":
                value = date.fromisoformat(str(value)[:10]).isoformat()
            else:
                value = str(value)
        except ValueError:
            raise HTTPException(400, f"{fl['label']}: invalid {t} value {value!r}")
        if t == "select" and value not in fl["options"]:
            raise HTTPException(400, f"{fl['label']} must be one of: {', '.join(fl['options'])}")
        out[name] = value
    if not partial:
        for fl in fields:
            if fl["name"] not in out and "default" in fl:
                out[fl["name"]] = fl["default"]
            if fl.get("required") and out.get(fl["name"]) is None:
                raise HTTPException(400, f"{fl['label']} is required")
    return out


def _integrity(e):
    if e.kind == "foreign_key":
        return HTTPException(409, "This record is linked to other records (e.g. commitments, "
                                  "capital events or investments). Remove those first.")
    if e.kind == "unique":
        return HTTPException(409, "A record with these values already exists "
                                  "(each LP can have one commitment per fund).")
    return HTTPException(400, "Invalid value for this record.")


def check_rules(conn, entity, row):
    """Cross-record rules, checked on the full new record (create, or old merged with changes)."""
    if entity == "rounds":
        invested = conn.execute("SELECT 1 FROM investments WHERE company_id = ? LIMIT 1",
                                (row["company_id"],)).fetchone()
        if not invested:
            raise HTTPException(400, "Rounds are tracked for portfolio companies. Record an "
                                     "investment in this company first.")
    if entity == "investments" and row.get("round_id") is not None:
        rnd = conn.execute("SELECT company_id FROM rounds WHERE id = ?",
                           (row["round_id"],)).fetchone()
        if rnd and rnd["company_id"] != row["company_id"]:
            raise HTTPException(400, "The linked round belongs to a different company.")
    if entity == "tasks" and (row.get("entity") is None) != (row.get("entity_id") is None):
        raise HTTPException(400, "Choose both the related record type and the record, or neither.")
    if entity == "documents" and not re.match(r"^https?://[^\s]+$", row.get("url") or ""):
        raise HTTPException(400, "Document links must be web addresses starting with https://")


# For each record type, the parent records whose history should include its changes.
PARENT_KEYS = {
    "commitments": [("lps", "lp_id"), ("funds", "fund_id")],
    "investments": [("companies", "company_id"), ("funds", "fund_id"), ("rounds", "round_id")],
    "valuations": [("companies", "company_id"), ("funds", "fund_id")],
    "realizations": [("companies", "company_id"), ("funds", "fund_id")],
    "capital_events": [("funds", "fund_id")],
    "rounds": [("companies", "company_id")],
    "round_investors": [("rounds", "round_id"), ("investors", "investor_id")],
}


def related(entity, row):
    if not row:
        return set()
    out = {(e, row.get(k)) for e, k in PARENT_KEYS.get(entity, []) if row.get(k) is not None}
    if entity in ("activities", "documents", "tasks", "contact_links") and row.get("entity"):
        out.add((row["entity"], row.get("entity_id")))
    if entity == "contact_links":
        out.add(("contacts", row.get("contact_id")))
    return out


def detach(conn, entity, ids_sql, args):
    """Remove notes, document links and person links attached to records being deleted;
    tasks about them are kept but no longer point at a record."""
    for table in ("activities", "documents", "contact_links"):
        conn.execute(f"DELETE FROM {table} WHERE entity = ? AND entity_id IN ({ids_sql})",
                     (entity, *args))
    conn.execute(f"UPDATE tasks SET entity = NULL, entity_id = NULL WHERE entity = ? "
                 f"AND entity_id IN ({ids_sql})", (entity, *args))


def audit(conn, user, action, entity, entity_id, changes=None, record=None, also=()):
    """Log a change. `record` is a snapshot of the record (used to label the entry and to
    decide who may see it later); `also` adds parents beyond those in the snapshot, e.g.
    the old LP when a commitment moves."""
    aid = conn.insert("audit_log", {
        "user_id": user["id"], "action": action, "entity": entity, "entity_id": entity_id,
        "changes": json.dumps(changes, default=str) if changes is not None else None,
        "record": json.dumps(record, default=str) if record is not None else None,
    })
    links = (set(also) | related(entity, record)) - {(entity, entity_id)}
    conn.executemany("INSERT INTO audit_links (audit_id, entity, entity_id) VALUES (?, ?, ?)",
                     [(aid, e, i) for e, i in links if i is not None])


def public_user(u):
    return {k: u[k] for k in ("id", "email", "name", "role")}


SECRET_USER_FIELDS = {"password_hash", "totp_secret", "totp_pending_secret", "totp_last_counter"}


def safe_user(row):
    return {k: v for k, v in row.items() if k not in SECRET_USER_FIELDS}


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def require_2fa(conn):
    return get_setting(conn, "require_2fa") == "1"


def create_app(database_url=None, cookie_secure=None, session_days=None, send_email=None,
               start_jobs=False):
    db = Database(database_url)
    db.init()
    cookie_secure = env_flag("COOKIE_SECURE") if cookie_secure is None else cookie_secure
    session_days = session_days or int(os.environ.get("SESSION_DAYS", "7"))
    throttle = auth.LoginThrottle()
    send_email = send_email or mailer.send
    bootstrap_admin(db)
    if start_jobs:
        jobs.start(db, send_email)

    app = FastAPI(title="VC CRM", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_conn():
        conn = db.connect()
        try:
            yield conn
        finally:
            conn.close()

    def check_csrf(request):
        # Browsers cannot attach a custom header cross-site without a CORS
        # preflight, which this app never grants, so this blocks forged writes.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-crm") != "1":
            raise HTTPException(403, "Missing X-CRM request header")

    def current_user(request: Request, conn=Depends(get_conn)):
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            # API tokens are not sent automatically by browsers, so no CSRF check needed.
            user = auth.token_user(conn, bearer[7:].strip())
            if not user:
                raise HTTPException(401, "Invalid or revoked API token")
        else:
            check_csrf(request)
            user = auth.session_user(conn, request.cookies.get(auth.COOKIE))
            if not user:
                raise HTTPException(401, "Not signed in")
        if (not user["totp_enabled"] and not request.url.path.startswith("/api/auth/")
                and require_2fa(conn)):
            raise HTTPException(403, "Two-factor authentication must be set up first")
        return user

    def role(minimum):
        def dep(user=Depends(current_user)):
            if auth.ROLE_RANK[user["role"]] < auth.ROLE_RANK[minimum]:
                raise HTTPException(403, f"This action needs the {minimum} role")
            return user
        return dep

    viewer, admin = role("viewer"), role("admin")

    def get_access(user=Depends(viewer), conn=Depends(get_conn)):
        return Access(conn, user)

    def entity_meta(entity):
        if entity not in ENTITIES:
            raise HTTPException(404, f"Unknown entity {entity!r}")
        return ENTITIES[entity]

    def fetch(conn, table, rid):
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        return dict(row)

    def start_session(response, conn, row):
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (auth.iso(auth.utcnow()), row["id"]))
        token = auth.create_session(conn, row["id"], session_days)
        response.set_cookie(auth.COOKIE, token, max_age=session_days * 86400, httponly=True,
                            secure=cookie_secure, samesite="lax", path="/")
        return {**public_user(row), **Access(conn, row).summary()}

    def notify_assignee(conn, task, actor):
        """Email someone when a task is assigned to them by someone else (best effort)."""
        if not task.get("assignee_id") or task["assignee_id"] == actor["id"]:
            return
        to = conn.execute("SELECT name, email, active FROM users WHERE id = ?",
                          (task["assignee_id"],)).fetchone()
        if not to or not to["active"]:
            return
        due = f"\nDue: {task['due_date']}" if task.get("due_date") else ""
        send_email(to["email"], f"New task: {task['title']}", (
            f"Hi {to['name']},\n\n{actor['name']} assigned you a task in VC CRM:\n\n"
            f"{task['title']}{due}\n\nOpen your tasks: {mailer.app_url()}/#tasks"))

    # Shared helpers handed to the feature modules that register their own routes.
    deps = SimpleNamespace(
        get_conn=get_conn, viewer=viewer, admin=admin, get_access=get_access, fetch=fetch,
        check_csrf=check_csrf, start_session=start_session, throttle=throttle, audit=audit,
        send_email=send_email, cookie_secure=cookie_secure, coerce=coerce,
        entity_meta=entity_meta, related=related, integrity=_integrity,
    )

    # ------------------------------------------------------------ health

    @app.get("/healthz")
    def healthz(conn=Depends(get_conn)):
        conn.execute("SELECT 1").fetchone()
        return {"ok": True}

    # ------------------------------------------------------------ auth

    @app.post("/api/auth/login")
    def login(request: Request, response: Response, data: dict = Body(...),
              conn=Depends(get_conn)):
        check_csrf(request)
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        keys = (f"email:{email}", f"ip:{request.client.host if request.client else '?'}")
        if throttle.blocked(*keys):
            raise HTTPException(429, "Too many failed sign-in attempts. Try again in 15 minutes.")
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        ok = auth.verify_password(password, row["password_hash"] if row else auth._DUMMY_HASH)
        if not (row and ok and row["active"]):
            throttle.fail(*keys)
            raise HTTPException(401, "Invalid email or password")
        throttle.clear(keys[0])
        if row["totp_enabled"]:
            # Password is right; the session starts only after the authenticator code.
            challenge = security.new_challenge(conn, row["id"])
            return {"twofa_required": True, "challenge": challenge}
        return start_session(response, conn, row)

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response, conn=Depends(get_conn)):
        check_csrf(request)
        token = request.cookies.get(auth.COOKIE)
        if token:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (auth.token_hash(token),))
        response.delete_cookie(auth.COOKIE, path="/")

    @app.patch("/api/auth/preferences")
    def preferences(data: dict = Body(...), user=Depends(viewer), conn=Depends(get_conn)):
        if set(data) - {"email_reminders"}:
            raise HTTPException(400, "Unknown preference")
        conn.execute("UPDATE users SET email_reminders = ? WHERE id = ?",
                     (1 if data.get("email_reminders") else 0, user["id"]))
        return {"email_reminders": bool(data.get("email_reminders"))}

    @app.get("/api/auth/me")
    def me(acc=Depends(get_access), conn=Depends(get_conn)):
        required = require_2fa(conn)
        prefs = conn.execute("SELECT email_reminders FROM users WHERE id = ?",
                             (acc.user["id"],)).fetchone()
        return {**public_user(acc.user), **acc.summary(),
                "email_reminders": bool(prefs["email_reminders"]) if prefs else True,
                "totp_enabled": bool(acc.user["totp_enabled"]), "require_2fa": required,
                "must_enroll_2fa": required and not acc.user["totp_enabled"]}

    @app.post("/api/auth/password", status_code=204)
    def change_password(request: Request, data: dict = Body(...), user=Depends(viewer),
                        conn=Depends(get_conn)):
        row = fetch(conn, "users", user["id"])
        if not auth.verify_password(str(data.get("current_password", "")), row["password_hash"]):
            raise HTTPException(400, "Current password is incorrect")
        new = data.get("new_password")
        if problem := auth.check_password_policy(new):
            raise HTTPException(400, problem)
        keep = auth.token_hash(request.cookies.get(auth.COOKIE, ""))
        with conn.transaction():
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (auth.hash_password(new), user["id"]))
            # Sign out every other device.
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?",
                         (user["id"], keep))
            audit(conn, user, "password_change", "users", user["id"])

    # ------------------------------------------------------------ user admin

    def active_admins(conn, excluding=None):
        return conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 AND id <> ?",
            (excluding or 0,)).fetchone()["n"]

    @app.get("/api/users")
    def list_users(user=Depends(admin), conn=Depends(get_conn)):
        grants = {}
        for r in conn.execute("SELECT user_id, fund_id, level FROM fund_access"):
            grants.setdefault(r["user_id"], {})[str(r["fund_id"])] = r["level"]
        return [{**dict(r), "fund_access": grants.get(r["id"], {})} for r in conn.execute(
            "SELECT id, email, name, role, active, totp_enabled, last_login_at, created_at "
            "FROM users "
            "ORDER BY name")]

    @app.get("/api/users/directory")
    def user_directory(user=Depends(viewer), conn=Depends(get_conn)):
        return [dict(r) for r in conn.execute("SELECT id, name FROM users ORDER BY name")]

    @app.post("/api/users", status_code=201)
    def create_user(data: dict = Body(...), user=Depends(admin), conn=Depends(get_conn)):
        email = str(data.get("email", "")).strip().lower()
        name = str(data.get("name", "")).strip()
        role_ = data.get("role", "viewer")
        if "@" not in email or not name:
            raise HTTPException(400, "Name and a valid email are required")
        if role_ not in auth.ROLES:
            raise HTTPException(400, f"Role must be one of: {', '.join(auth.ROLES)}")
        if problem := auth.check_password_policy(data.get("password")):
            raise HTTPException(400, problem)
        try:
            with conn.transaction():
                uid = conn.insert("users", {"email": email, "name": name, "role": role_,
                                            "password_hash": auth.hash_password(data["password"])})
                audit(conn, user, "create", "users", uid, {"email": email, "name": name,
                                                           "role": role_})
        except IntegrityError:
            raise HTTPException(409, "A user with that email already exists")
        return safe_user(fetch(conn, "users", uid))

    @app.patch("/api/users/{uid}")
    def update_user(uid: int, data: dict = Body(...), user=Depends(admin),
                    conn=Depends(get_conn)):
        target = fetch(conn, "users", uid)
        unknown = set(data) - {"name", "role", "active", "password", "reset_2fa"}
        if unknown:
            raise HTTPException(400, f"Unknown field(s): {', '.join(sorted(unknown))}")
        changes = {}
        if "name" in data:
            if not str(data["name"]).strip():
                raise HTTPException(400, "Name is required")
            changes["name"] = str(data["name"]).strip()
        if "role" in data:
            if data["role"] not in auth.ROLES:
                raise HTTPException(400, f"Role must be one of: {', '.join(auth.ROLES)}")
            changes["role"] = data["role"]
        if "active" in data:
            changes["active"] = 1 if data["active"] in (True, 1, "1", "true") else 0
        still_admin = (changes.get("role", target["role"]) == "admin"
                       and changes.get("active", target["active"]) == 1)
        if target["role"] == "admin" and not still_admin and active_admins(conn, uid) == 0:
            raise HTTPException(400, "At least one active admin is required")
        revoke = changes.get("active") == 0
        logged = dict(changes)
        if "password" in data:
            if problem := auth.check_password_policy(data["password"]):
                raise HTTPException(400, problem)
            changes["password_hash"] = auth.hash_password(data["password"])
            logged["password"] = "reset"
            revoke = True
        if data.get("reset_2fa"):
            # Lost phone: clear the authenticator; they set it up again at next sign-in.
            changes.update(totp_enabled=0, totp_secret=None, totp_pending_secret=None,
                           totp_last_counter=None)
            logged = {k: v for k, v in logged.items() if not k.startswith("totp")}
            logged["two_factor"] = "reset"
            revoke = True
        if changes:
            with conn.transaction():
                conn.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in changes)} "
                             f"WHERE id = ?", [*changes.values(), uid])
                if revoke:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
                if data.get("reset_2fa"):
                    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (uid,))
                audit(conn, user, "update", "users", uid, logged)
        return safe_user(fetch(conn, "users", uid))

    @app.get("/api/users/{uid}/funds")
    def get_fund_access(uid: int, user=Depends(admin), conn=Depends(get_conn)):
        fetch(conn, "users", uid)
        return {str(r["fund_id"]): r["level"] for r in conn.execute(
            "SELECT fund_id, level FROM fund_access WHERE user_id = ?", (uid,))}

    @app.put("/api/users/{uid}/funds")
    def set_fund_access(uid: int, data: dict = Body(...), user=Depends(admin),
                        conn=Depends(get_conn)):
        """Replace a user's fund access with {fund_id: "viewer" | "editor"}; omit for none."""
        fetch(conn, "users", uid)
        grants = {}
        for fid, level in data.items():
            if level in (None, "", "none"):
                continue
            if level not in LEVELS:
                raise HTTPException(400, f"Level must be one of: {', '.join(LEVELS)}")
            try:
                grants[int(fid)] = level
            except ValueError:
                raise HTTPException(400, f"Invalid fund id {fid!r}")
        try:
            with conn.transaction():
                conn.execute("DELETE FROM fund_access WHERE user_id = ?", (uid,))
                conn.executemany(
                    "INSERT INTO fund_access (user_id, fund_id, level) VALUES (?, ?, ?)",
                    [(uid, fid, lvl) for fid, lvl in grants.items()])
                audit(conn, user, "update", "users", uid,
                      {"fund_access": {str(k): v for k, v in grants.items()}})
        except IntegrityError:
            raise HTTPException(400, "Unknown fund")
        return get_fund_access(uid, user, conn)

    @app.get("/api/audit")
    def audit_log(entity: str | None = None, entity_id: int | None = None, limit: int = 50,
                  before: int | None = None, related: bool = True,
                  acc=Depends(get_access), conn=Depends(get_conn)):
        """Newest first. For one record, also includes changes to its child records
        (related=true), each shown only if the user may see that child. Page with
        `before` = the id of the last entry received."""
        if not acc.admin:
            # Non-admins read history one record at a time, for records they can see.
            if entity not in ENTITIES and entity != "capital_events" or entity_id is None:
                raise deny("Only admins can browse the full change log.")
            visible_row(conn, acc, entity, entity_id)
        where, args = [], []
        if entity and entity_id is not None and related:
            where.append("((a.entity = ? AND a.entity_id = ?) OR a.id IN "
                         "(SELECT audit_id FROM audit_links WHERE entity = ? AND entity_id = ?))")
            args += [entity, entity_id, entity, entity_id]
        else:
            if entity:
                where.append("a.entity = ?")
                args.append(entity)
            if entity_id is not None:
                where.append("a.entity_id = ?")
                args.append(entity_id)
        limit = max(1, min(limit, 500))
        out, cursor = [], before
        while len(out) < limit:
            page_where = where + (["a.id < ?"] if cursor else [])
            sql = ("SELECT a.*, u.name AS user_name FROM audit_log a "
                   "LEFT JOIN users u ON u.id = a.user_id")
            if page_where:
                sql += " WHERE " + " AND ".join(page_where)
            rows = conn.execute(f"{sql} ORDER BY a.id DESC LIMIT 200",
                                [*args, *([cursor] if cursor else [])]).fetchall()
            if not rows:
                break
            for r in rows:
                e = {**dict(r), "changes": json.loads(r["changes"]) if r["changes"] else None,
                     "record": json.loads(r["record"]) if r["record"] else None}
                direct = e["entity"] == entity and e["entity_id"] == entity_id
                if acc.admin or direct or (e["record"] and e["entity"] != "users"
                                           and acc.can_view(e["entity"], e["record"])):
                    out.append(e)
                    if len(out) == limit:
                        break
            cursor = rows[-1]["id"]
        return out

    # ------------------------------------------------------------ metadata

    @app.get("/api/meta")
    def meta(user=Depends(viewer)):
        return {
            "entities": ENTITIES,
            "capital_event_fields": CAPITAL_EVENT_FIELDS,
            "lp_stages": LP_STAGES,
            "deal_stages": DEAL_STAGES,
            "roles": auth.ROLES,
            "fund_levels": LEVELS,
        }

    # ------------------------------------------------------------ analytics
    # Every figure is computed only over funds the user can see.

    def visible_funds(conn, acc):
        rows = conn.execute("SELECT * FROM funds ORDER BY vintage DESC NULLS LAST, name").fetchall()
        return [f for f in rows if acc.can_view_fund(f["id"])]

    def check_fund_filter(acc, fund_id):
        if fund_id is not None and not acc.can_view_fund(fund_id):
            raise HTTPException(404, "Fund not found")

    def visible_holdings(conn, acc, fund_id=None):
        return [h for h in metrics.holdings(conn, fund_id) if acc.can_view_fund(h["fund_id"])]

    @app.get("/api/dashboard")
    def dashboard(acc=Depends(get_access), conn=Depends(get_conn)):
        funds = [metrics.fund_summary(conn, f) for f in visible_funds(conn, acc)]
        rows = visible_holdings(conn, acc)
        companies = [dict(c) for c in conn.execute("SELECT * FROM companies")]
        companies = [c for c in companies if acc.can_view("companies", c)]
        deals = {}
        for c in companies:
            deals[c["deal_stage"]] = deals.get(c["deal_stage"], 0) + 1
        horizon = (date.today() + timedelta(days=14)).isoformat()
        visible_co = {c["id"] for c in companies}
        tasks = [dict(r) for r in conn.execute(
            """
            SELECT 'commitments' AS entity, c.id, c.fund_id, l.name || ' / ' || f.name AS title,
                   c.next_step, c.next_step_date, c.owner, c.stage
            FROM commitments c JOIN lps l ON l.id = c.lp_id JOIN funds f ON f.id = c.fund_id
            WHERE c.next_step_date IS NOT NULL AND c.next_step_date <= ?
              AND c.stage NOT IN ('Closed', 'Declined')
            UNION ALL
            SELECT 'companies', id, fund_id, name, next_step, next_step_date, deal_owner, deal_stage
            FROM companies
            WHERE next_step_date IS NOT NULL AND next_step_date <= ?
              AND deal_stage NOT IN ('Invested', 'Passed')
            ORDER BY next_step_date
            """, (horizon, horizon))]
        tasks = [t for t in tasks if (acc.can_view_fund(t["fund_id"]) if t["entity"] == "commitments"
                                      else t["id"] in visible_co)]
        for t in tasks:
            t.pop("fund_id")
        return {
            "funds": funds,
            "portfolio": metrics.portfolio_totals(rows),
            "deal_pipeline": {s: deals.get(s, 0) for s in DEAL_STAGES},
            "tasks": tasks,
            "today": date.today().isoformat(),
        }

    @app.get("/api/portfolio")
    def portfolio(fund_id: int | None = None, acc=Depends(get_access), conn=Depends(get_conn)):
        check_fund_filter(acc, fund_id)
        rows = visible_holdings(conn, acc, fund_id)
        totals = metrics.portfolio_totals(rows)
        for r in rows:
            r.pop("_flows")
        return {"holdings": rows, "totals": totals}

    @app.get("/api/funds/summary")
    def funds_summary(acc=Depends(get_access), conn=Depends(get_conn)):
        return [acc.annotate("funds", metrics.fund_summary(conn, f))
                for f in visible_funds(conn, acc)]

    @app.get("/api/lp-positions")
    def lp_positions(fund_id: int | None = None, acc=Depends(get_access),
                     conn=Depends(get_conn)):
        check_fund_filter(acc, fund_id)
        return [p for p in metrics.lp_positions(conn, fund_id) if acc.can_view_fund(p["fund_id"])]

    # ------------------------------------------------------------ capital events

    def visible_event(conn, acc, eid):
        ev = fetch(conn, "capital_events", eid)
        if not acc.can_view_fund(ev["fund_id"]):
            raise HTTPException(404, "Not found")
        return ev

    def need_fund_edit(acc, fund_id):
        if not acc.can_edit_fund(fund_id):
            raise deny("You need editor access to this fund.")

    @app.get("/api/capital-events")
    def list_events(fund_id: int | None = None, acc=Depends(get_access), conn=Depends(get_conn)):
        check_fund_filter(acc, fund_id)
        where, args = ("WHERE e.fund_id = ?", [fund_id]) if fund_id else ("", [])
        rows = [dict(r) for r in conn.execute(
            f"""SELECT e.*, f.name AS fund,
                       (SELECT COUNT(*) FROM capital_event_allocations a
                        WHERE a.event_id = e.id) AS lp_count,
                       (SELECT COUNT(*) FROM capital_event_allocations a
                        WHERE a.event_id = e.id AND a.paid = 1) AS paid_count
                FROM capital_events e JOIN funds f ON f.id = e.fund_id {where}
                ORDER BY e.event_date DESC, e.id DESC""", args)]
        return [acc.annotate("capital_events", r) for r in rows
                if acc.can_view_fund(r["fund_id"])]

    @app.post("/api/capital-events", status_code=201)
    def create_event(data: dict = Body(...), acc=Depends(get_access), conn=Depends(get_conn)):
        row = coerce(CAPITAL_EVENT_FIELDS, data, partial=False)
        need_fund_edit(acc, row["fund_id"])
        if row["total_amount"] <= 0:
            raise HTTPException(400, "Total amount must be positive")
        with conn.transaction():
            commits = conn.execute(
                "SELECT id, committed_amount FROM commitments "
                "WHERE fund_id = ? AND stage = 'Closed' AND committed_amount > 0",
                (row["fund_id"],)).fetchall()
            if not commits:
                raise HTTPException(400, "This fund has no closed commitments with a committed "
                                         "amount, so there is no one to allocate to.")
            if row["event_type"] == "Capital call":
                called = conn.execute(
                    "SELECT COALESCE(SUM(total_amount), 0) AS n FROM capital_events "
                    "WHERE fund_id = ? AND event_type = 'Capital call'",
                    (row["fund_id"],)).fetchone()["n"]
                committed = sum(c["committed_amount"] for c in commits)
                if called + row["total_amount"] > committed + 0.005:
                    raise HTTPException(400, f"Call exceeds uncalled capital "
                                             f"({committed - called:,.2f} remaining).")
            split = metrics.allocate(row["total_amount"],
                                     {c["id"]: c["committed_amount"] for c in commits})
            eid = conn.insert("capital_events", row)
            conn.executemany(
                "INSERT INTO capital_event_allocations (event_id, commitment_id, amount) "
                "VALUES (?, ?, ?)", [(eid, cid, amt) for cid, amt in split.items()])
            audit(conn, acc.user, "create", "capital_events", eid, row, record={**row, "id": eid})
        return acc.annotate("capital_events", fetch(conn, "capital_events", eid))

    @app.get("/api/capital-events/{eid}/allocations")
    def event_allocations(eid: int, acc=Depends(get_access), conn=Depends(get_conn)):
        visible_event(conn, acc, eid)
        return [dict(r) for r in conn.execute(
            """SELECT a.*, l.name AS lp, c.committed_amount AS committed
               FROM capital_event_allocations a
               JOIN commitments c ON c.id = a.commitment_id
               JOIN lps l ON l.id = c.lp_id
               WHERE a.event_id = ? ORDER BY l.name""", (eid,))]

    @app.patch("/api/allocations/{aid}")
    def mark_paid(aid: int, data: dict = Body(...), acc=Depends(get_access),
                  conn=Depends(get_conn)):
        alloc = fetch(conn, "capital_event_allocations", aid)
        ev = visible_event(conn, acc, alloc["event_id"])
        need_fund_edit(acc, ev["fund_id"])
        paid = 1 if data.get("paid") else 0
        with conn.transaction():
            conn.execute("UPDATE capital_event_allocations SET paid = ? WHERE id = ?", (paid, aid))
            lp = conn.execute("SELECT l.name FROM commitments c JOIN lps l ON l.id = c.lp_id "
                              "WHERE c.id = ?", (alloc["commitment_id"],)).fetchone()
            audit(conn, acc.user, "update", "capital_events", alloc["event_id"],
                  {"paid": {"from": alloc["paid"], "to": paid}, "lp": lp["name"] if lp else None},
                  record=ev)
        return fetch(conn, "capital_event_allocations", aid)

    @app.delete("/api/capital-events/{eid}", status_code=204)
    def delete_event(eid: int, acc=Depends(get_access), conn=Depends(get_conn)):
        old = visible_event(conn, acc, eid)
        need_fund_edit(acc, old["fund_id"])
        with conn.transaction():
            detach(conn, "capital_events", "?", (eid,))
            conn.execute("DELETE FROM capital_events WHERE id = ?", (eid,))
            audit(conn, acc.user, "delete", "capital_events", eid, old, record=old)

    # ------------------------------------------------------------ feature modules
    # Registered before the generic /api/{entity} routes so their paths win.

    deps.visible_row = lambda conn, acc, entity, rid: visible_row(conn, acc, entity, rid)
    security.register(app, deps)
    search.register(app, deps)
    data_io.register(app, deps)

    # ------------------------------------------------------------ generic CRUD

    def visible_row(conn, acc, entity, rid):
        row = fetch(conn, entity, rid)
        if not acc.can_view(entity, row):
            raise HTTPException(404, "Not found")  # do not reveal that it exists
        return row

    @app.get("/api/{entity}")
    def list_rows(entity: str, request: Request, acc=Depends(get_access),
                  conn=Depends(get_conn)):
        m = entity_meta(entity)
        names = {fl["name"] for fl in m["fields"]}
        filters = {k: v for k, v in request.query_params.items() if k in names}
        where = " AND ".join(f"{k} = ?" for k in filters)
        sql = f"SELECT * FROM {entity}" + (f" WHERE {where}" if where else "")
        try:
            rows = [dict(r) for r in conn.execute(f"{sql} ORDER BY {m['order']}",
                                                  list(filters.values()))]
        except IntegrityError:
            raise HTTPException(400, "Invalid filter value")
        return [acc.annotate(entity, r) for r in rows if acc.can_view(entity, r)]

    @app.get("/api/{entity}/{rid}")
    def get_row(entity: str, rid: int, acc=Depends(get_access), conn=Depends(get_conn)):
        entity_meta(entity)
        return acc.annotate(entity, visible_row(conn, acc, entity, rid))

    @app.post("/api/{entity}", status_code=201)
    def create_row(entity: str, data: dict = Body(...), acc=Depends(get_access),
                   conn=Depends(get_conn)):
        row = coerce(entity_meta(entity)["fields"], data, partial=False)
        if entity in ("activities", "documents", "contact_links", "tasks") \
                and row.get("entity") is not None and not acc.parent(row):
            raise HTTPException(400, "The record this is attached to does not exist")
        if entity in ("activities", "documents", "tasks"):
            row["created_by"] = acc.user["id"]
        if entity == "tasks" and row.get("status") == "Done":
            row["completed_at"] = auth.iso(auth.utcnow())
        if not acc.can_create(entity, row):
            raise deny("Only admins can create funds." if entity == "funds" else
                       "You need editor access to this fund (or record) to add that.")
        check_rules(conn, entity, row)
        try:
            with conn.transaction():
                rid = conn.insert(entity, row)
                audit(conn, acc.user, "create", entity, rid, row, record={**row, "id": rid})
        except IntegrityError as e:
            raise _integrity(e)
        acc.reset()
        if entity == "tasks":
            notify_assignee(conn, {**row, "id": rid}, acc.user)
        return acc.annotate(entity, fetch(conn, entity, rid))

    @app.patch("/api/{entity}/{rid}")
    def update_row(entity: str, rid: int, data: dict = Body(...), acc=Depends(get_access),
                   conn=Depends(get_conn)):
        row = coerce(entity_meta(entity)["fields"], data, partial=True)
        old = visible_row(conn, acc, entity, rid)
        if not acc.can_edit(entity, old):
            raise deny("You have view-only access to this record.")
        changed = {k: v for k, v in row.items() if old.get(k) != v}
        moves = {"fund_id", "entity", "entity_id", "company_id", "round_id"} & set(changed)
        if moves:
            # Moving a record to another fund (or parent) needs edit rights on both ends.
            if not acc.can_create(entity, {**old, **changed}):
                raise deny("You need editor access to the fund (or record) you are moving this to.")
            if entity == "companies" and old["fund_id"] is not None \
                    and not acc.can_edit_fund(old["fund_id"]):
                raise deny("You need editor access to this company's current fund to change it.")
        if changed:
            check_rules(conn, entity, {**old, **changed})
        if entity == "tasks" and "status" in changed:
            changed["completed_at"] = auth.iso(auth.utcnow()) if changed["status"] == "Done" else None
        if changed:
            try:
                with conn.transaction():
                    conn.execute(
                        f"UPDATE {entity} SET {', '.join(f'{k} = ?' for k in changed)} "
                        f"WHERE id = ?", [*changed.values(), rid])
                    audit(conn, acc.user, "update", entity, rid,
                          {k: {"from": old.get(k), "to": v} for k, v in changed.items()},
                          record={**old, **changed}, also=related(entity, old))
            except IntegrityError as e:
                raise _integrity(e)
            acc.reset()
            if entity == "tasks" and "assignee_id" in changed:
                notify_assignee(conn, {**old, **changed}, acc.user)
        return acc.annotate(entity, fetch(conn, entity, rid))

    @app.delete("/api/{entity}/{rid}", status_code=204)
    def delete_row(entity: str, rid: int, acc=Depends(get_access), conn=Depends(get_conn)):
        entity_meta(entity)
        old = visible_row(conn, acc, entity, rid)
        if not acc.can_delete(entity, old):
            raise deny("Only admins can delete funds." if entity == "funds" else
                       "Deleting this also deletes data in funds you cannot edit."
                       if entity in ("lps", "companies") and acc.can_edit(entity, old) else
                       "You have view-only access to this record.")
        try:
            with conn.transaction():
                if entity in PARENTS:
                    detach(conn, entity, "?", (rid,))
                    if entity == "lps":
                        detach(conn, "commitments",
                               "SELECT id FROM commitments WHERE lp_id = ?", (rid,))
                    if entity == "companies":
                        detach(conn, "rounds", "SELECT id FROM rounds WHERE company_id = ?", (rid,))
                conn.execute(f"DELETE FROM {entity} WHERE id = ?", (rid,))
                audit(conn, acc.user, "delete", entity, rid, old, record=old)
        except IntegrityError as e:
            raise _integrity(e)

    # ------------------------------------------------------------ UI

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def bootstrap_admin(db):
    """Create the first admin from ADMIN_EMAIL / ADMIN_PASSWORD when there are no users."""
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or not password:
        return
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]:
            return
        if problem := auth.check_password_policy(password):
            raise SystemExit(f"ADMIN_PASSWORD rejected: {problem}")
        conn.insert("users", {"email": email, "name": os.environ.get("ADMIN_NAME", "Admin"),
                              "role": "admin", "password_hash": auth.hash_password(password)})
    finally:
        conn.close()
