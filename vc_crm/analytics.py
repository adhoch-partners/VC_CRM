"""Pipeline analytics for the LP and deal pipelines, rebuilt from the change history.

Every stage change is in the audit log, so for each LP commitment or deal we can replay
the stages it went through and when. From that: how many reached each stage, conversion
between stages, how long things sit in each stage, win rate, and time to close.
"""

import json
from datetime import datetime, timezone
from statistics import median

from fastapi import Depends, HTTPException

from .entities import DEAL_STAGES, LP_STAGES

KINDS = {
    # kind: (table, stage field, ordered stages, won stage, lost stage)
    "lp": ("commitments", "stage", LP_STAGES, "Closed", "Declined"),
    "deal": ("companies", "deal_stage", DEAL_STAGES, "Invested", "Passed"),
}


def parse(ts):
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def days(a, b):
    return (b - a).total_seconds() / 86400


def stays(record, stage_field, events, now):
    """[(stage, start, end or None)] for one record, oldest first."""
    created = parse(record["created_at"])
    transitions = []
    start_stage = None
    for e in events:
        ch = json.loads(e["changes"]) if e["changes"] else {}
        if e["action"] == "create":
            start_stage = ch.get(stage_field) or start_stage
            created = parse(e["at"])
        elif e["action"] == "update" and isinstance(ch.get(stage_field), dict):
            transitions.append((parse(e["at"]), ch[stage_field].get("from"), ch[stage_field].get("to")))
    if start_stage is None:
        start_stage = transitions[0][1] if transitions else record[stage_field]
    out, stage, since = [], start_stage, created
    for at, _from, to in transitions:
        out.append((stage, since, at))
        stage, since = to, at
    out.append((stage, since, None))
    return out


def summarize(records, stage_field, stages, won, lost, history, now, extra=None):
    order = [s for s in stages if s != lost]
    rank = {s: i for i, s in enumerate(order)}
    reached = [0] * len(order)
    completed = {s: [] for s in stages}
    current = {s: [] for s in stages}
    lost_after = {s: 0 for s in order}
    to_win = []
    n_won = n_lost = 0
    for r in records:
        path = stays(r, stage_field, history.get(r["id"], []), now)
        visited = [s for s, _, _ in path if s in rank]
        if visited:
            for i in range(max(rank[s] for s in visited) + 1):
                reached[i] += 1
        for stage, start, end in path:
            if stage not in completed:
                continue
            if end is None:
                current[stage].append(days(start, now))
            else:
                completed[stage].append(days(start, end))
        final = path[-1][0]
        if final == won:
            n_won += 1
            to_win.append(days(path[0][1], path[-1][1]))
        elif final == lost:
            n_lost += 1
            before = [s for s, _, _ in path if s in rank]
            if before:
                lost_after[before[-1]] += 1
    rows = []
    for i, s in enumerate(order):
        nxt = reached[i + 1] if i + 1 < len(order) else None
        rows.append({
            "stage": s,
            "reached": reached[i],
            "conversion_to_next": (nxt / reached[i]) if (nxt is not None and reached[i]) else None,
            "median_days": round(median(completed[s]), 1) if completed[s] else None,
            "completed_stays": len(completed[s]),
            "current": len(current[s]),
            "current_median_age": round(median(current[s]), 1) if current[s] else None,
            "lost_here": lost_after[s],
        })
    out = {
        "stages": rows,
        "total": len(records),
        "won": n_won,
        "lost": n_lost,
        "win_rate": n_won / (n_won + n_lost) if (n_won + n_lost) else None,
        "median_days_to_win": round(median(to_win), 1) if to_win else None,
    }
    if extra:
        out.update(extra)
    return out


def register(app, d):
    get_conn, get_access = d.get_conn, d.get_access

    @app.get("/api/analytics/pipeline")
    def pipeline(kind: str = "lp", fund_id: int | None = None, acc=Depends(get_access),
                 conn=Depends(get_conn)):
        if kind not in KINDS:
            raise HTTPException(400, "kind must be 'lp' or 'deal'")
        if fund_id is not None and not acc.can_view_fund(fund_id):
            raise HTTPException(404, "Fund not found")
        table, field, stages, won, lost = KINDS[kind]
        records = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
        records = [r for r in records if acc.can_view(table, r)
                   and (fund_id is None or r.get("fund_id") == fund_id)]
        history = {}
        ids = {r["id"] for r in records}
        for e in conn.execute("SELECT entity_id, action, changes, at FROM audit_log "
                              "WHERE entity = ? ORDER BY id", (table,)):
            if e["entity_id"] in ids:
                history.setdefault(e["entity_id"], []).append(dict(e))
        extra = None
        if kind == "deal":
            reasons = {}
            for r in records:
                if r["deal_stage"] == lost:
                    key = (r.get("pass_reason") or "No reason given").strip()
                    reasons[key] = reasons.get(key, 0) + 1
            extra = {"pass_reasons": sorted(reasons.items(), key=lambda x: -x[1])}
        return summarize(records, field, stages, won, lost, history,
                         datetime.now(timezone.utc), extra)
