import json
import os
from datetime import date, timedelta
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import auth, metrics
from .db import Database, IntegrityError
from .entities import CAPITAL_EVENT_FIELDS, DEAL_STAGES, ENTITIES, LP_STAGES

STATIC = Path(__file__).with_name("static")

# Records that own activity-log rows; deleting one also deletes its log.
ACTIVITY_OWNERS = {"lps", "commitments", "companies", "funds"}

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


def audit(conn, user, action, entity, entity_id, changes=None):
    conn.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, changes) VALUES (?, ?, ?, ?, ?)",
        (user["id"], action, entity, entity_id,
         json.dumps(changes, default=str) if changes is not None else None))


def public_user(u):
    return {k: u[k] for k in ("id", "email", "name", "role")}


def create_app(database_url=None, cookie_secure=None, session_days=None):
    db = Database(database_url)
    db.init()
    cookie_secure = env_flag("COOKIE_SECURE") if cookie_secure is None else cookie_secure
    session_days = session_days or int(os.environ.get("SESSION_DAYS", "7"))
    throttle = auth.LoginThrottle()
    bootstrap_admin(db)

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
        check_csrf(request)
        user = auth.session_user(conn, request.cookies.get(auth.COOKIE))
        if not user:
            raise HTTPException(401, "Not signed in")
        return user

    def role(minimum):
        def dep(user=Depends(current_user)):
            if auth.ROLE_RANK[user["role"]] < auth.ROLE_RANK[minimum]:
                raise HTTPException(403, f"This action needs the {minimum} role")
            return user
        return dep

    viewer, editor, admin = role("viewer"), role("editor"), role("admin")

    def entity_meta(entity):
        if entity not in ENTITIES:
            raise HTTPException(404, f"Unknown entity {entity!r}")
        return ENTITIES[entity]

    def fetch(conn, table, rid):
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        return dict(row)

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
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (auth.iso(auth.utcnow()), row["id"]))
        token = auth.create_session(conn, row["id"], session_days)
        response.set_cookie(auth.COOKIE, token, max_age=session_days * 86400, httponly=True,
                            secure=cookie_secure, samesite="lax", path="/")
        return public_user(row)

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response, conn=Depends(get_conn)):
        check_csrf(request)
        token = request.cookies.get(auth.COOKIE)
        if token:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (auth.token_hash(token),))
        response.delete_cookie(auth.COOKIE, path="/")

    @app.get("/api/auth/me")
    def me(user=Depends(viewer)):
        return public_user(user)

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
        return [dict(r) for r in conn.execute(
            "SELECT id, email, name, role, active, last_login_at, created_at FROM users "
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
        return {k: v for k, v in fetch(conn, "users", uid).items() if k != "password_hash"}

    @app.patch("/api/users/{uid}")
    def update_user(uid: int, data: dict = Body(...), user=Depends(admin),
                    conn=Depends(get_conn)):
        target = fetch(conn, "users", uid)
        unknown = set(data) - {"name", "role", "active", "password"}
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
        if changes:
            with conn.transaction():
                conn.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in changes)} "
                             f"WHERE id = ?", [*changes.values(), uid])
                if revoke:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
                audit(conn, user, "update", "users", uid, logged)
        return {k: v for k, v in fetch(conn, "users", uid).items() if k != "password_hash"}

    @app.get("/api/audit")
    def audit_log(entity: str | None = None, entity_id: int | None = None, limit: int = 50,
                  user=Depends(viewer), conn=Depends(get_conn)):
        where, args = [], []
        if entity:
            where.append("a.entity = ?")
            args.append(entity)
        if entity_id is not None:
            where.append("a.entity_id = ?")
            args.append(entity_id)
        sql = ("SELECT a.*, u.name AS user_name FROM audit_log a "
               "LEFT JOIN users u ON u.id = a.user_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = conn.execute(f"{sql} ORDER BY a.id DESC LIMIT ?",
                            [*args, max(1, min(limit, 500))])
        return [{**dict(r), "changes": json.loads(r["changes"]) if r["changes"] else None}
                for r in rows]

    # ------------------------------------------------------------ metadata

    @app.get("/api/meta")
    def meta(user=Depends(viewer)):
        return {
            "entities": ENTITIES,
            "capital_event_fields": CAPITAL_EVENT_FIELDS,
            "lp_stages": LP_STAGES,
            "deal_stages": DEAL_STAGES,
            "roles": auth.ROLES,
        }

    # ------------------------------------------------------------ analytics

    def all_funds(conn):
        return conn.execute("SELECT * FROM funds ORDER BY vintage DESC NULLS LAST, name").fetchall()

    @app.get("/api/dashboard")
    def dashboard(user=Depends(viewer), conn=Depends(get_conn)):
        funds = [metrics.fund_summary(conn, f) for f in all_funds(conn)]
        rows = metrics.holdings(conn)
        deals = {r["deal_stage"]: r["n"] for r in conn.execute(
            "SELECT deal_stage, COUNT(*) AS n FROM companies GROUP BY deal_stage")}
        horizon = (date.today() + timedelta(days=14)).isoformat()
        tasks = [dict(r) for r in conn.execute(
            """
            SELECT 'commitments' AS entity, c.id, l.name || ' / ' || f.name AS title,
                   c.next_step, c.next_step_date, c.owner, c.stage
            FROM commitments c JOIN lps l ON l.id = c.lp_id JOIN funds f ON f.id = c.fund_id
            WHERE c.next_step_date IS NOT NULL AND c.next_step_date <= ?
              AND c.stage NOT IN ('Closed', 'Declined')
            UNION ALL
            SELECT 'companies', id, name, next_step, next_step_date, deal_owner, deal_stage
            FROM companies
            WHERE next_step_date IS NOT NULL AND next_step_date <= ?
              AND deal_stage NOT IN ('Invested', 'Passed')
            ORDER BY next_step_date
            """, (horizon, horizon))]
        return {
            "funds": funds,
            "portfolio": metrics.portfolio_totals(rows),
            "deal_pipeline": {s: deals.get(s, 0) for s in DEAL_STAGES},
            "tasks": tasks,
            "today": date.today().isoformat(),
        }

    @app.get("/api/portfolio")
    def portfolio(fund_id: int | None = None, user=Depends(viewer), conn=Depends(get_conn)):
        rows = metrics.holdings(conn, fund_id)
        totals = metrics.portfolio_totals(rows)
        for r in rows:
            r.pop("_flows")
        return {"holdings": rows, "totals": totals}

    @app.get("/api/funds/summary")
    def funds_summary(user=Depends(viewer), conn=Depends(get_conn)):
        return [metrics.fund_summary(conn, f) for f in all_funds(conn)]

    @app.get("/api/lp-positions")
    def lp_positions(fund_id: int | None = None, user=Depends(viewer), conn=Depends(get_conn)):
        return metrics.lp_positions(conn, fund_id)

    # ------------------------------------------------------------ capital events

    @app.get("/api/capital-events")
    def list_events(fund_id: int | None = None, user=Depends(viewer), conn=Depends(get_conn)):
        where, args = ("WHERE e.fund_id = ?", [fund_id]) if fund_id else ("", [])
        return [dict(r) for r in conn.execute(
            f"""SELECT e.*, f.name AS fund,
                       (SELECT COUNT(*) FROM capital_event_allocations a
                        WHERE a.event_id = e.id) AS lp_count,
                       (SELECT COUNT(*) FROM capital_event_allocations a
                        WHERE a.event_id = e.id AND a.paid = 1) AS paid_count
                FROM capital_events e JOIN funds f ON f.id = e.fund_id {where}
                ORDER BY e.event_date DESC, e.id DESC""", args)]

    @app.post("/api/capital-events", status_code=201)
    def create_event(data: dict = Body(...), user=Depends(editor), conn=Depends(get_conn)):
        row = coerce(CAPITAL_EVENT_FIELDS, data, partial=False)
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
            audit(conn, user, "create", "capital_events", eid, row)
        return fetch(conn, "capital_events", eid)

    @app.get("/api/capital-events/{eid}/allocations")
    def event_allocations(eid: int, user=Depends(viewer), conn=Depends(get_conn)):
        fetch(conn, "capital_events", eid)
        return [dict(r) for r in conn.execute(
            """SELECT a.*, l.name AS lp, c.committed_amount AS committed
               FROM capital_event_allocations a
               JOIN commitments c ON c.id = a.commitment_id
               JOIN lps l ON l.id = c.lp_id
               WHERE a.event_id = ? ORDER BY l.name""", (eid,))]

    @app.patch("/api/allocations/{aid}")
    def mark_paid(aid: int, data: dict = Body(...), user=Depends(editor),
                  conn=Depends(get_conn)):
        alloc = fetch(conn, "capital_event_allocations", aid)
        paid = 1 if data.get("paid") else 0
        with conn.transaction():
            conn.execute("UPDATE capital_event_allocations SET paid = ? WHERE id = ?", (paid, aid))
            audit(conn, user, "update", "capital_events", alloc["event_id"],
                  {"allocation_id": aid, "paid": paid})
        return fetch(conn, "capital_event_allocations", aid)

    @app.delete("/api/capital-events/{eid}", status_code=204)
    def delete_event(eid: int, user=Depends(editor), conn=Depends(get_conn)):
        old = fetch(conn, "capital_events", eid)
        with conn.transaction():
            conn.execute("DELETE FROM capital_events WHERE id = ?", (eid,))
            audit(conn, user, "delete", "capital_events", eid, old)

    # ------------------------------------------------------------ generic CRUD

    @app.get("/api/{entity}")
    def list_rows(entity: str, request: Request, user=Depends(viewer), conn=Depends(get_conn)):
        m = entity_meta(entity)
        names = {fl["name"] for fl in m["fields"]}
        filters = {k: v for k, v in request.query_params.items() if k in names}
        where = " AND ".join(f"{k} = ?" for k in filters)
        sql = f"SELECT * FROM {entity}" + (f" WHERE {where}" if where else "")
        try:
            return [dict(r) for r in conn.execute(f"{sql} ORDER BY {m['order']}",
                                                  list(filters.values()))]
        except IntegrityError:
            raise HTTPException(400, "Invalid filter value")

    @app.get("/api/{entity}/{rid}")
    def get_row(entity: str, rid: int, user=Depends(viewer), conn=Depends(get_conn)):
        entity_meta(entity)
        return fetch(conn, entity, rid)

    @app.post("/api/{entity}", status_code=201)
    def create_row(entity: str, data: dict = Body(...), user=Depends(editor),
                   conn=Depends(get_conn)):
        row = coerce(entity_meta(entity)["fields"], data, partial=False)
        if entity == "activities":
            row["created_by"] = user["id"]
        try:
            with conn.transaction():
                rid = conn.insert(entity, row)
                audit(conn, user, "create", entity, rid, row)
        except IntegrityError as e:
            raise _integrity(e)
        return fetch(conn, entity, rid)

    @app.patch("/api/{entity}/{rid}")
    def update_row(entity: str, rid: int, data: dict = Body(...), user=Depends(editor),
                   conn=Depends(get_conn)):
        row = coerce(entity_meta(entity)["fields"], data, partial=True)
        old = fetch(conn, entity, rid)
        changed = {k: v for k, v in row.items() if old.get(k) != v}
        if changed:
            try:
                with conn.transaction():
                    conn.execute(
                        f"UPDATE {entity} SET {', '.join(f'{k} = ?' for k in changed)} "
                        f"WHERE id = ?", [*changed.values(), rid])
                    audit(conn, user, "update", entity, rid,
                          {k: {"from": old.get(k), "to": v} for k, v in changed.items()})
            except IntegrityError as e:
                raise _integrity(e)
        return fetch(conn, entity, rid)

    @app.delete("/api/{entity}/{rid}", status_code=204)
    def delete_row(entity: str, rid: int, user=Depends(editor), conn=Depends(get_conn)):
        entity_meta(entity)
        old = fetch(conn, entity, rid)
        try:
            with conn.transaction():
                if entity in ACTIVITY_OWNERS:
                    conn.execute("DELETE FROM activities WHERE entity = ? AND entity_id = ?",
                                 (entity, rid))
                    if entity == "lps":
                        conn.execute(
                            "DELETE FROM activities WHERE entity = 'commitments' AND entity_id "
                            "IN (SELECT id FROM commitments WHERE lp_id = ?)", (rid,))
                conn.execute(f"DELETE FROM {entity} WHERE id = ?", (rid,))
                audit(conn, user, "delete", entity, rid, old)
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
