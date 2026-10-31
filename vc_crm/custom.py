"""Admin-defined custom fields.

A custom field is stored as a definition in `custom_fields` and its values in the record's
`custom` JSON column, keyed "cf_<key>". Everywhere else (forms, grids, validation, history,
import/export) it behaves like a built-in field, because `fields_for` merges it into the
record type's field list.
"""

import json
import re

from fastapi import Body, Depends, HTTPException

from .entities import ENTITIES

CUSTOMIZABLE = ["lps", "companies", "investors", "contacts", "commitments", "funds", "rounds"]
TYPES = {"text": "Text", "longtext": "Long text", "number": "Number", "money": "Amount ($)",
         "percent": "Percent", "date": "Date", "select": "Choice list"}
PREFIX = "cf_"


def definitions(conn, entity=None):
    sql, args = "SELECT * FROM custom_fields", []
    if entity:
        sql, args = sql + " WHERE entity = ?", [entity]
    return [dict(r) for r in conn.execute(sql + " ORDER BY entity, position, id", args)]


def as_field(d):
    f = {"name": PREFIX + d["key"], "label": d["label"], "type": d["field_type"], "custom": True,
         "custom_id": d["id"]}
    if d["field_type"] == "select":
        f["options"] = json.loads(d["options"] or "[]")
    return f


def fields_for(conn, entity):
    base = ENTITIES[entity]["fields"]
    if entity not in CUSTOMIZABLE:
        return base
    return base + [as_field(d) for d in definitions(conn, entity)]


def all_custom(conn):
    out = {}
    for d in definitions(conn):
        out.setdefault(d["entity"], []).append(as_field(d))
    return out


def expand(entity, row):
    """Turn the stored JSON into cf_* keys on the row (in place) and return it."""
    if entity in CUSTOMIZABLE and "custom" in row:
        raw = row.pop("custom")
        if raw:
            row.update({k: v for k, v in json.loads(raw).items() if k.startswith(PREFIX)})
    return row


def split(row):
    """Remove cf_* keys from a write and return them."""
    return {k: row.pop(k) for k in list(row) if k.startswith(PREFIX)}


def merged_json(old, changes):
    values = {k: v for k, v in old.items() if k.startswith(PREFIX)}
    values.update(changes)
    return json.dumps({k: v for k, v in values.items() if v is not None})


def slug(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]


def register(app, d):
    get_conn, admin, viewer = d.get_conn, d.admin, d.viewer

    def check(data, partial):
        if not partial or "label" in data:
            if not str(data.get("label", "")).strip():
                raise HTTPException(400, "Give the field a name")
        if "field_type" in data and data["field_type"] not in TYPES:
            raise HTTPException(400, f"Type must be one of: {', '.join(TYPES)}")
        opts = data.get("options")
        if opts is not None:
            if isinstance(opts, str):
                opts = [o.strip() for o in opts.split(",")]
            opts = [str(o).strip() for o in opts if str(o).strip()]
            if len(set(opts)) != len(opts):
                raise HTTPException(400, "Choices must be unique")
            data["options"] = opts
        return data

    @app.get("/api/custom-fields")
    def list_fields(user=Depends(viewer), conn=Depends(get_conn)):
        return [{**f, "options": json.loads(f["options"]) if f["options"] else None}
                for f in definitions(conn)]

    @app.post("/api/custom-fields", status_code=201)
    def create_field(data: dict = Body(...), user=Depends(admin), conn=Depends(get_conn)):
        entity = data.get("entity")
        if entity not in CUSTOMIZABLE:
            raise HTTPException(400, f"Custom fields can be added to: {', '.join(CUSTOMIZABLE)}")
        data = check(dict(data), partial=False)
        ftype = data.get("field_type", "text")
        if ftype not in TYPES:
            raise HTTPException(400, f"Type must be one of: {', '.join(TYPES)}")
        if ftype == "select" and not data.get("options"):
            raise HTTPException(400, "A choice list needs at least one choice")
        key = slug(data["label"])
        if not key:
            raise HTTPException(400, "Use letters or numbers in the field name")
        # Custom keys are prefixed, so only other custom fields can clash.
        taken = {x["key"] for x in definitions(conn, entity)}
        base, n = key, 2
        while key in taken:
            key, n = f"{base}_{n}", n + 1
        row = {"entity": entity, "key": key, "label": data["label"].strip(), "field_type": ftype,
               "options": json.dumps(data["options"]) if ftype == "select" else None,
               "position": len(definitions(conn, entity))}
        with conn.transaction():
            fid = conn.insert("custom_fields", row)
            d.audit(conn, user, "create", "custom_fields", fid, row)
        return as_field({**row, "id": fid})

    @app.patch("/api/custom-fields/{fid}")
    def update_field(fid: int, data: dict = Body(...), user=Depends(admin), conn=Depends(get_conn)):
        cur = d.fetch(conn, "custom_fields", fid)
        if set(data) - {"label", "options", "position"}:
            raise HTTPException(400, "Only the name, choices and position can change "
                                     "(the type is fixed once data may exist)")
        data = check(dict(data), partial=True)
        changes = {}
        if "label" in data:
            changes["label"] = data["label"].strip()
        if "options" in data:
            if cur["field_type"] != "select":
                raise HTTPException(400, "Only choice lists have choices")
            changes["options"] = json.dumps(data["options"])
        if "position" in data:
            changes["position"] = int(data["position"])
        if changes:
            with conn.transaction():
                conn.execute(f"UPDATE custom_fields SET {', '.join(f'{k} = ?' for k in changes)} "
                             "WHERE id = ?", [*changes.values(), fid])
                d.audit(conn, user, "update", "custom_fields", fid, changes)
        return as_field(d.fetch(conn, "custom_fields", fid))

    @app.delete("/api/custom-fields/{fid}", status_code=204)
    def delete_field(fid: int, user=Depends(admin), conn=Depends(get_conn)):
        """Removes the field from the app. Stored values stay in the database (and in the
        change history), so re-creating a field with the same name brings them back."""
        cur = d.fetch(conn, "custom_fields", fid)
        with conn.transaction():
            conn.execute("DELETE FROM custom_fields WHERE id = ?", (fid,))
            d.audit(conn, user, "delete", "custom_fields", fid, cur)

    # ------------------------------------------------------------ saved views

    @app.get("/api/saved-views")
    def list_views(view: str, user=Depends(viewer), conn=Depends(get_conn)):
        rows = conn.execute(
            "SELECT v.*, u.name AS owner FROM saved_views v JOIN users u ON u.id = v.user_id "
            "WHERE v.view = ? AND (v.user_id = ? OR v.shared = 1) ORDER BY v.name",
            (view, user["id"])).fetchall()
        return [{**dict(r), "state": json.loads(r["state"]), "mine": r["user_id"] == user["id"]}
                for r in rows]

    @app.post("/api/saved-views", status_code=201)
    def save_view(data: dict = Body(...), user=Depends(viewer), conn=Depends(get_conn)):
        name = str(data.get("name", "")).strip()
        view = str(data.get("view", "")).strip()
        if not name or not view or not isinstance(data.get("state"), dict):
            raise HTTPException(400, "A view needs a name, the screen it belongs to, and its state")
        state = json.dumps(data["state"])
        if len(state) > 20_000:
            raise HTTPException(400, "View is too large")
        with conn.transaction():
            # Saving under an existing name of yours replaces it.
            conn.execute("DELETE FROM saved_views WHERE user_id = ? AND view = ? AND name = ?",
                         (user["id"], view, name))
            vid = conn.insert("saved_views", {"user_id": user["id"], "view": view, "name": name,
                                              "state": state,
                                              "shared": 1 if data.get("shared") else 0})
        return {"id": vid, "name": name, "view": view, "state": data["state"],
                "shared": bool(data.get("shared")), "mine": True}

    @app.delete("/api/saved-views/{vid}", status_code=204)
    def delete_view(vid: int, user=Depends(viewer), conn=Depends(get_conn)):
        row = d.fetch(conn, "saved_views", vid)
        if row["user_id"] != user["id"] and user["role"] != "admin":
            raise HTTPException(403, "Only the person who saved a view (or an admin) can delete it")
        conn.execute("DELETE FROM saved_views WHERE id = ?", (vid,))
