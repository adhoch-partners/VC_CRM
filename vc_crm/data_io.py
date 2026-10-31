"""CSV export (any list, or everything as a zip for admins) and CSV import with a preview.

Exported and imported amounts are full dollars (not $000s), because the files usually
go to spreadsheets, accountants and fund administrators. Linked records are written and
read by name: "Fund" = "Fund I", "Round" = "Acme · Series A".
"""

import csv
import io
import re
import zipfile
from datetime import date, datetime

from fastapi import Body, Depends, HTTPException
from fastapi.responses import Response

from .entities import CAPITAL_EVENT_FIELDS, ENTITIES
from .search import NAMED, find_duplicates, normalize

# Record types that can be imported, in the order they usually need to be loaded.
IMPORTABLE = ["funds", "lps", "investors", "companies", "commitments", "investments",
              "valuations", "realizations", "rounds", "round_investors", "contacts"]
MAX_ROWS = 5000
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y"]


def fields_of(entity):
    return CAPITAL_EVENT_FIELDS if entity == "capital_events" else ENTITIES[entity]["fields"]


class Names:
    """id <-> display name for every record type a field can link to."""

    def __init__(self, conn):
        self.conn = conn
        self._by_id = {}

    def table(self, ref):
        if ref not in self._by_id:
            if ref == "rounds":
                rows = self.conn.execute(
                    "SELECT r.id, c.name || ' · ' || r.round_name AS name FROM rounds r "
                    "JOIN companies c ON c.id = r.company_id")
            else:
                rows = self.conn.execute(f"SELECT id, name FROM {ref}")
            self._by_id[ref] = {r["id"]: r["name"] for r in rows}
        return self._by_id[ref]

    def name(self, ref, rid):
        return self.table(ref).get(rid, "") if rid is not None else ""

    def find(self, ref, text, acc):
        """ids of visible `ref` records whose name matches `text` (ignoring case/punctuation)."""
        want = normalize(text)
        hits = [rid for rid, n in self.table(ref).items() if normalize(n) == want]
        if acc.admin or ref in ("investors",):
            return hits
        out = []
        for rid in hits:
            row = self.conn.execute(f"SELECT * FROM {ref} WHERE id = ?", (rid,)).fetchone()
            if row and acc.can_view(ref, dict(row)):
                out.append(rid)
        return out


def to_csv(rows, fields, names, extra=()):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", *(f["label"] for f in fields), *(label for label, _ in extra)])
    for r in rows:
        out = [r["id"]]
        for f in fields:
            v = r.get(f["name"])
            out.append(names.name(f["ref"], v) if f["type"] == "ref" else ("" if v is None else v))
        out += [fn(r) for _, fn in extra]
        w.writerow(out)
    return buf.getvalue()


def export_rows(conn, acc, entity):
    order = ENTITIES[entity]["order"] if entity in ENTITIES else "id"
    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {entity} ORDER BY {order}")]
    return [r for r in rows if acc.can_view(entity, r)]


def parse_number(text):
    t = text.replace("$", "").replace(",", "").replace("%", "").strip()
    if t.startswith("(") and t.endswith(")"):  # accounting negative
        t = "-" + t[1:-1]
    return t


def parse_date(text):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return text  # let validation report it


def header_map(headers, fields):
    """CSV column index -> field, matching on label or internal name, any case."""
    lookup = {}
    for f in fields:
        lookup[f["name"].lower()] = f
        lookup[f["label"].lower()] = f
        lookup[re.sub(r"\s*\(.*\)$", "", f["label"].lower())] = f  # "Target size ($000s)"
    mapping, unknown = {}, []
    for i, h in enumerate(headers):
        key = h.strip().lower()
        if key in ("id", ""):
            continue
        if key in lookup and lookup[key] not in mapping.values():
            mapping[i] = lookup[key]
        else:
            unknown.append(h.strip())
    return mapping, unknown


def register(app, d):
    get_conn, get_access, admin = d.get_conn, d.get_access, d.admin

    def check_entity(entity, allowed):
        if entity not in allowed or (entity not in ENTITIES and entity != "capital_events"):
            raise HTTPException(404, f"Cannot import/export {entity!r}")

    # ------------------------------------------------------------ export

    @app.get("/api/export/{entity}.csv")
    def export_csv(entity: str, acc=Depends(get_access), conn=Depends(get_conn)):
        check_entity(entity, set(ENTITIES) | {"capital_events"})
        body = to_csv(export_rows(conn, acc, entity), fields_of(entity), Names(conn))
        return Response(body, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{entity}-{date.today()}.csv"'})

    @app.get("/api/export/all.zip")
    def export_all(user=Depends(admin), acc=Depends(get_access), conn=Depends(get_conn)):
        """Every record type as CSV, plus capital call allocations and the change log."""
        names = Names(conn)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for entity in [*ENTITIES, "capital_events"]:
                z.writestr(f"{entity}.csv", to_csv(export_rows(conn, acc, entity),
                                                   fields_of(entity), names))
            allocs = [dict(r) for r in conn.execute(
                "SELECT a.*, e.fund_id, e.event_type, e.event_date, c.lp_id "
                "FROM capital_event_allocations a JOIN capital_events e ON e.id = a.event_id "
                "JOIN commitments c ON c.id = a.commitment_id ORDER BY a.id")]
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["ID", "Capital event ID", "Fund", "Type", "Date", "LP", "Amount", "Paid"])
            for a in allocs:
                w.writerow([a["id"], a["event_id"], names.name("funds", a["fund_id"]),
                            a["event_type"], a["event_date"], names.name("lps", a["lp_id"]),
                            a["amount"], "Yes" if a["paid"] else "No"])
            z.writestr("capital_event_allocations.csv", out.getvalue())
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["ID", "When (UTC)", "User", "Action", "Record type", "Record ID", "Changes"])
            for r in conn.execute("SELECT a.*, u.name AS user_name FROM audit_log a "
                                  "LEFT JOIN users u ON u.id = a.user_id ORDER BY a.id"):
                w.writerow([r["id"], r["at"], r["user_name"] or "", r["action"], r["entity"],
                            r["entity_id"] if r["entity_id"] is not None else "",
                            r["changes"] or ""])
            z.writestr("change_log.csv", out.getvalue())
        return Response(buf.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="vc-crm-export-{date.today()}.zip"'})

    # ------------------------------------------------------------ import

    @app.get("/api/import/{entity}/template.csv")
    def import_template(entity: str, acc=Depends(get_access)):
        check_entity(entity, IMPORTABLE)
        cols = [f["label"] for f in fields_of(entity)]
        return Response(",".join(f'"{c}"' for c in cols) + "\n", media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{entity}-template.csv"'})

    @app.post("/api/import/{entity}")
    def import_csv(entity: str, data: dict = Body(...), acc=Depends(get_access),
                   conn=Depends(get_conn)):
        """Preview (commit=false) or import (commit=true) CSV text. Nothing is written unless
        every row is valid, or skip_errors=true. Likely duplicates are skipped unless
        skip_duplicates=false."""
        from .app import check_rules
        check_entity(entity, IMPORTABLE)
        text = str(data.get("csv", "")).lstrip("﻿")
        if len(text) > 5_000_000:
            raise HTTPException(400, "File is too large (5 MB maximum). Split it into parts.")
        rows = list(csv.reader(io.StringIO(text)))
        rows = [r for r in rows if any(c.strip() for c in r)]
        if len(rows) < 2:
            raise HTTPException(400, "The file needs a header row and at least one data row.")
        if len(rows) - 1 > MAX_ROWS:
            raise HTTPException(400, f"At most {MAX_ROWS} rows per import.")
        fields = fields_of(entity)
        mapping, unknown = header_map(rows[0], fields)
        if not mapping:
            raise HTTPException(400, "None of the column headers match this record type. "
                                     "Download the template to see the expected headers.")
        names = Names(conn)
        seen_names = {}
        results, ready = [], []
        for n, raw in enumerate(rows[1:], start=2):
            values, problems = {}, []
            for i, f in mapping.items():
                cell = raw[i].strip() if i < len(raw) else ""
                if cell == "":
                    continue
                t = f["type"]
                if t in ("money", "number", "percent", "integer"):
                    cell = parse_number(cell)
                elif t == "date":
                    cell = parse_date(cell)
                elif t == "select":
                    match = [o for o in f["options"] if o.lower() == cell.lower()]
                    cell = match[0] if match else cell
                elif t == "ref":
                    ids = names.find(f["ref"], cell, acc)
                    if len(ids) != 1:
                        problems.append(f"{f['label']}: {'no' if not ids else 'more than one'} "
                                        f"match for '{cell}'")
                        continue
                    cell = ids[0]
                values[f["name"]] = cell
            row = None
            if not problems:
                try:
                    row = d.coerce(fields, values, partial=False)
                    if not acc.can_create(entity, row):
                        problems.append("You don't have edit access for this record's fund")
                    else:
                        check_rules(conn, entity, row)
                except HTTPException as e:
                    problems.append(str(e.detail))
            status, message = ("error", "; ".join(problems)) if problems else ("ok", "")
            if status == "ok" and entity in NAMED:
                key = normalize(row["name"])
                dup = find_duplicates(conn, acc, entity, row["name"])
                if key in seen_names:
                    status, message = "duplicate", f"Same name as row {seen_names[key]}"
                elif dup["visible"]:
                    status = "duplicate"
                    message = "Looks like existing " + ", ".join(x["name"] for x in dup["visible"])
                elif dup["hidden_count"]:
                    status = "duplicate"
                    message = "A similar record exists in a fund you can't see"
                seen_names.setdefault(key, n)
            label = values.get("name") or values.get(fields[0]["name"])
            if fields[0]["type"] == "ref" and isinstance(label, int):
                label = names.name(fields[0]["ref"], label)
            results.append({"row": n, "status": status, "message": message, "label": label})
            ready.append((status, row))
        counts = {s: sum(1 for r in results if r["status"] == s) for s in ("ok", "duplicate", "error")}
        summary = {"columns": [f["label"] for f in mapping.values()], "ignored_columns": unknown,
                   "rows": results, "counts": counts, "imported": 0}
        if not data.get("commit"):
            return summary
        if counts["error"] and not data.get("skip_errors"):
            raise HTTPException(400, f"{counts['error']} row(s) have errors. Fix them, or choose "
                                     "to import only the valid rows.")
        take = {"ok"} if data.get("skip_duplicates", True) else {"ok", "duplicate"}
        with conn.transaction():
            for status, row in ready:
                if status in take:
                    rid = conn.insert(entity, row)
                    d.audit(conn, acc.user, "create", entity, rid, {**row, "imported": True},
                            record={**row, "id": rid})
                    summary["imported"] += 1
        return summary
