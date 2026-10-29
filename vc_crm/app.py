import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import metrics
from .db import DEFAULT_DB, connect, init_db
from .entities import CAPITAL_EVENT_FIELDS, DEAL_STAGES, ENTITIES, LP_STAGES

STATIC = Path(__file__).with_name("static")

# Records that own activity-log rows; deleting one also deletes its log.
ACTIVITY_OWNERS = {"lps", "commitments", "companies", "funds"}


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
    msg = str(e)
    if "FOREIGN KEY" in msg:
        return HTTPException(409, "This record is linked to other records (e.g. commitments, "
                                  "capital events or investments). Remove those first.")
    if "UNIQUE" in msg:
        return HTTPException(409, "A record with these values already exists "
                                  "(each LP can have one commitment per fund).")
    return HTTPException(400, msg)


def create_app(db_path=DEFAULT_DB):
    init_db(db_path)
    app = FastAPI(title="VC CRM")

    def get_conn():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def entity_meta(entity):
        if entity not in ENTITIES:
            raise HTTPException(404, f"Unknown entity {entity!r}")
        return ENTITIES[entity]

    def fetch(conn, table, rid):
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        return dict(row)

    # ------------------------------------------------------------ metadata

    @app.get("/api/meta")
    def meta():
        return {
            "entities": ENTITIES,
            "capital_event_fields": CAPITAL_EVENT_FIELDS,
            "lp_stages": LP_STAGES,
            "deal_stages": DEAL_STAGES,
        }

    # ------------------------------------------------------------ analytics

    @app.get("/api/dashboard")
    def dashboard(conn=Depends(get_conn)):
        funds = [metrics.fund_summary(conn, f) for f in
                 conn.execute("SELECT * FROM funds ORDER BY vintage DESC, name")]
        rows = metrics.holdings(conn)
        deals = {r["deal_stage"]: r["n"] for r in conn.execute(
            "SELECT deal_stage, COUNT(*) n FROM companies GROUP BY deal_stage")}
        horizon = (date.today() + timedelta(days=14)).isoformat()
        tasks = [dict(r) for r in conn.execute(
            """
            SELECT 'commitments' entity, c.id, l.name || ' / ' || f.name title,
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
    def portfolio(fund_id: int | None = None, conn=Depends(get_conn)):
        rows = metrics.holdings(conn, fund_id)
        totals = metrics.portfolio_totals(rows)
        for r in rows:
            r.pop("_flows")
        return {"holdings": rows, "totals": totals}

    @app.get("/api/funds/summary")
    def funds_summary(conn=Depends(get_conn)):
        return [metrics.fund_summary(conn, f) for f in
                conn.execute("SELECT * FROM funds ORDER BY vintage DESC, name")]

    @app.get("/api/lp-positions")
    def lp_positions(fund_id: int | None = None, conn=Depends(get_conn)):
        return metrics.lp_positions(conn, fund_id)

    # ------------------------------------------------------------ capital events

    @app.get("/api/capital-events")
    def list_events(fund_id: int | None = None, conn=Depends(get_conn)):
        where, args = ("WHERE e.fund_id = ?", [fund_id]) if fund_id else ("", [])
        return [dict(r) for r in conn.execute(
            f"""SELECT e.*, f.name fund,
                       (SELECT COUNT(*) FROM capital_event_allocations a
                        WHERE a.event_id = e.id) lp_count,
                       (SELECT COUNT(*) FROM capital_event_allocations a
                        WHERE a.event_id = e.id AND a.paid = 1) paid_count
                FROM capital_events e JOIN funds f ON f.id = e.fund_id {where}
                ORDER BY e.event_date DESC, e.id DESC""", args)]

    @app.post("/api/capital-events", status_code=201)
    def create_event(data: dict = Body(...), conn=Depends(get_conn)):
        row = coerce(CAPITAL_EVENT_FIELDS, data, partial=False)
        if row["total_amount"] <= 0:
            raise HTTPException(400, "Total amount must be positive")
        commits = conn.execute(
            "SELECT id, committed_amount FROM commitments "
            "WHERE fund_id = ? AND stage = 'Closed' AND committed_amount > 0",
            (row["fund_id"],)).fetchall()
        if not commits:
            raise HTTPException(400, "This fund has no closed commitments with a committed "
                                     "amount, so there is no one to allocate to.")
        if row["event_type"] == "Capital call":
            called = conn.execute(
                "SELECT COALESCE(SUM(total_amount), 0) FROM capital_events "
                "WHERE fund_id = ? AND event_type = 'Capital call'",
                (row["fund_id"],)).fetchone()[0]
            committed = sum(c["committed_amount"] for c in commits)
            if called + row["total_amount"] > committed + 0.005:
                raise HTTPException(400, f"Call exceeds uncalled capital "
                                         f"({committed - called:,.2f} remaining).")
        split = metrics.allocate(row["total_amount"],
                                 {c["id"]: c["committed_amount"] for c in commits})
        with conn:
            cols = ", ".join(row)
            cur = conn.execute(
                f"INSERT INTO capital_events ({cols}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()))
            eid = cur.lastrowid
            conn.executemany(
                "INSERT INTO capital_event_allocations (event_id, commitment_id, amount) "
                "VALUES (?, ?, ?)", [(eid, cid, amt) for cid, amt in split.items()])
        return fetch(conn, "capital_events", eid)

    @app.get("/api/capital-events/{eid}/allocations")
    def event_allocations(eid: int, conn=Depends(get_conn)):
        fetch(conn, "capital_events", eid)
        return [dict(r) for r in conn.execute(
            """SELECT a.*, l.name lp, c.committed_amount committed
               FROM capital_event_allocations a
               JOIN commitments c ON c.id = a.commitment_id
               JOIN lps l ON l.id = c.lp_id
               WHERE a.event_id = ? ORDER BY l.name""", (eid,))]

    @app.patch("/api/allocations/{aid}")
    def mark_paid(aid: int, data: dict = Body(...), conn=Depends(get_conn)):
        fetch(conn, "capital_event_allocations", aid)
        with conn:
            conn.execute("UPDATE capital_event_allocations SET paid = ? WHERE id = ?",
                         (1 if data.get("paid") else 0, aid))
        return fetch(conn, "capital_event_allocations", aid)

    @app.delete("/api/capital-events/{eid}", status_code=204)
    def delete_event(eid: int, conn=Depends(get_conn)):
        fetch(conn, "capital_events", eid)
        with conn:
            conn.execute("DELETE FROM capital_events WHERE id = ?", (eid,))

    # ------------------------------------------------------------ generic CRUD

    @app.get("/api/{entity}")
    def list_rows(entity: str, request: Request, conn=Depends(get_conn)):
        m = entity_meta(entity)
        names = {fl["name"] for fl in m["fields"]}
        filters = {k: v for k, v in request.query_params.items() if k in names}
        where = " AND ".join(f"{k} = ?" for k in filters)
        sql = f"SELECT * FROM {entity}" + (f" WHERE {where}" if where else "")
        return [dict(r) for r in conn.execute(f"{sql} ORDER BY {m['order']}",
                                              list(filters.values()))]

    @app.get("/api/{entity}/{rid}")
    def get_row(entity: str, rid: int, conn=Depends(get_conn)):
        entity_meta(entity)
        return fetch(conn, entity, rid)

    @app.post("/api/{entity}", status_code=201)
    def create_row(entity: str, data: dict = Body(...), conn=Depends(get_conn)):
        row = coerce(entity_meta(entity)["fields"], data, partial=False)
        try:
            with conn:
                cur = conn.execute(
                    f"INSERT INTO {entity} ({', '.join(row)}) "
                    f"VALUES ({', '.join('?' * len(row))})", list(row.values()))
        except sqlite3.IntegrityError as e:
            raise _integrity(e)
        return fetch(conn, entity, cur.lastrowid)

    @app.patch("/api/{entity}/{rid}")
    def update_row(entity: str, rid: int, data: dict = Body(...), conn=Depends(get_conn)):
        row = coerce(entity_meta(entity)["fields"], data, partial=True)
        fetch(conn, entity, rid)
        if row:
            try:
                with conn:
                    conn.execute(
                        f"UPDATE {entity} SET {', '.join(f'{k} = ?' for k in row)} WHERE id = ?",
                        [*row.values(), rid])
            except sqlite3.IntegrityError as e:
                raise _integrity(e)
        return fetch(conn, entity, rid)

    @app.delete("/api/{entity}/{rid}", status_code=204)
    def delete_row(entity: str, rid: int, conn=Depends(get_conn)):
        entity_meta(entity)
        fetch(conn, entity, rid)
        try:
            with conn:
                if entity in ACTIVITY_OWNERS:
                    conn.execute("DELETE FROM activities WHERE entity = ? AND entity_id = ?",
                                 (entity, rid))
                    if entity == "lps":
                        conn.execute(
                            "DELETE FROM activities WHERE entity = 'commitments' AND entity_id "
                            "IN (SELECT id FROM commitments WHERE lp_id = ?)", (rid,))
                conn.execute(f"DELETE FROM {entity} WHERE id = ?", (rid,))
        except sqlite3.IntegrityError as e:
            raise _integrity(e)

    # ------------------------------------------------------------ UI

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app
