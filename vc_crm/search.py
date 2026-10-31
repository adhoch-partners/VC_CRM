"""Global search and duplicate-name warnings."""

import difflib
import re

from fastapi import Depends, HTTPException

# What to search, per record type: (columns to match, title column, subtitle columns).
SEARCHABLE = {
    "lps": (["name", "contact_name", "email", "tags", "location"], "name", ["lp_type", "location"]),
    "companies": (["name", "sector", "founders", "website"], "name", ["deal_stage", "sector"]),
    "investors": (["name", "contact_name", "email", "location"], "name", ["investor_type", "location"]),
    "funds": (["name"], "name", ["status"]),
    "contacts": (["name", "email", "title"], "name", ["title", "email"]),
}
# Record types whose names should not be duplicated.
NAMED = {"lps", "companies", "investors", "funds", "contacts"}

LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "lp", "llp", "plc", "corp",
                  "corporation", "co", "company", "gmbh", "sa", "ag", "bv", "the"}


def normalize(name):
    """'The Acme Endowment, Inc.' -> 'acme endowment'."""
    words = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower().replace("&", " and ")).split()
    return " ".join(w for w in words if w not in LEGAL_SUFFIXES)


def similar(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.88


def find_duplicates(conn, acc, entity, name, exclude_id=None):
    """Records of `entity` whose name looks like `name`. Hidden ones are only counted."""
    visible, hidden = [], 0
    for r in conn.execute(f"SELECT * FROM {entity}"):
        r = dict(r)
        if r["id"] == exclude_id or not similar(r["name"], name):
            continue
        if acc.can_view(entity, r):
            visible.append({"id": r["id"], "name": r["name"]})
        else:
            hidden += 1
    return {"visible": visible, "hidden_count": hidden}


def register(app, d):
    get_conn, get_access = d.get_conn, d.get_access

    @app.get("/api/search")
    def search(q: str = "", limit: int = 8, acc=Depends(get_access), conn=Depends(get_conn)):
        """Case-insensitive match across LPs, companies, investors, funds and rounds."""
        q = q.strip()
        if len(q) < 2:
            return []
        like = f"%{q.lower()}%"
        limit = max(1, min(limit, 25))
        out = []
        for entity, (cols, title, subs) in SEARCHABLE.items():
            where = " OR ".join(f"LOWER(COALESCE({c}, '')) LIKE ?" for c in cols)
            hits = []
            for r in conn.execute(f"SELECT * FROM {entity} WHERE {where} ORDER BY {title}",
                                  [like] * len(cols)):
                r = dict(r)
                if acc.can_view(entity, r):
                    hits.append({"entity": entity, "id": r["id"], "title": r[title],
                                 "subtitle": " · ".join(str(r[s]) for s in subs if r.get(s))})
                if len(hits) == limit:
                    break
            out += hits
        rounds = conn.execute(
            "SELECT r.*, c.name AS company FROM rounds r JOIN companies c ON c.id = r.company_id "
            "WHERE LOWER(c.name) LIKE ? OR LOWER(r.round_name) LIKE ? ORDER BY c.name",
            (like, like))
        hits = []
        for r in rounds:
            r = dict(r)
            if acc.can_view("rounds", r):
                hits.append({"entity": "rounds", "id": r["id"],
                             "title": f"{r['company']} · {r['round_name']}", "subtitle": r["status"]})
            if len(hits) == limit:
                break
        return out + hits

    @app.get("/api/duplicates")
    def duplicates(entity: str, name: str, exclude_id: int | None = None,
                   acc=Depends(get_access), conn=Depends(get_conn)):
        if entity not in NAMED:
            raise HTTPException(400, f"Duplicate checks are for {', '.join(sorted(NAMED))}")
        return find_duplicates(conn, acc, entity, name, exclude_id)
