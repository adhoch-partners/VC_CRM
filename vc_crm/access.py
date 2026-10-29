"""Per-fund permissions.

Admins see and edit everything. Everyone else gets a level per fund
(no row = no access, 'viewer', or 'editor'), stored in fund_access.

Fund-keyed records (commitments, investments, valuations, realizations,
capital events) follow the level on their fund.

LPs and companies are shared across funds. Their "linked funds" are the funds
they have commitments / a target fund / positions in. A user sees one when it
is linked to a fund they can see, or when it is linked to no fund at all
(new LPs, unassigned deals). They can edit it when they are an editor on a
linked fund, or when their general role is editor and they can see it.
Deleting one also deletes its fund-level children, so that additionally needs
editor access on every linked fund.

The user's general role (editor / viewer) only governs shared records.
"""

from fastapi import HTTPException

LEVELS = ["viewer", "editor"]
FUND_KEYED = {"commitments", "investments", "valuations", "realizations", "capital_events"}
SHARED = {"lps", "companies"}
PARENTS = {"lps", "commitments", "companies", "funds"}  # what activities can attach to


class Access:
    def __init__(self, conn, user):
        self.conn = conn
        self.user = user
        self.admin = user["role"] == "admin"
        self.general_edit = user["role"] in ("admin", "editor")
        self.levels = {} if self.admin else {
            r["fund_id"]: r["level"] for r in conn.execute(
                "SELECT fund_id, level FROM fund_access WHERE user_id = ?", (user["id"],))}
        self.reset()

    def reset(self):
        """Forget cached links; call after a write changes them."""
        self._lp_links = None
        self._company_links = None
        self._parents = {}

    # ---------------------------------------------------------------- funds

    def can_view_fund(self, fund_id):
        return self.admin or fund_id in self.levels

    def can_edit_fund(self, fund_id):
        return self.admin or self.levels.get(fund_id) == "editor"

    @property
    def any_fund_edit(self):
        return self.admin or "editor" in self.levels.values()

    @property
    def can_create_shared(self):
        return self.general_edit or self.any_fund_edit

    def summary(self):
        """What the browser needs to decide which create buttons to show."""
        return {
            "admin": self.admin,
            "fund_access": {str(k): v for k, v in self.levels.items()},
            "can_create_shared": self.can_create_shared,
        }

    # ---------------------------------------------------------------- links

    def links(self, entity, row):
        if entity == "lps":
            if self._lp_links is None:
                self._lp_links = {}
                for r in self.conn.execute("SELECT lp_id, fund_id FROM commitments"):
                    self._lp_links.setdefault(r["lp_id"], set()).add(r["fund_id"])
            return self._lp_links.get(row["id"], set())
        if self._company_links is None:
            self._company_links = {}
            for r in self.conn.execute(
                "SELECT id AS company_id, fund_id FROM companies WHERE fund_id IS NOT NULL "
                "UNION SELECT company_id, fund_id FROM investments "
                "UNION SELECT company_id, fund_id FROM valuations "
                "UNION SELECT company_id, fund_id FROM realizations"
            ):
                self._company_links.setdefault(r["company_id"], set()).add(r["fund_id"])
        found = set(self._company_links.get(row.get("id"), set()))
        if row.get("fund_id") is not None:
            found.add(row["fund_id"])
        return found

    def _shared_view(self, links):
        return self.admin or not links or any(self.can_view_fund(f) for f in links)

    def _shared_edit(self, links):
        if self.admin:
            return True
        if not links:
            return self.can_create_shared
        return (any(self.can_edit_fund(f) for f in links)
                or (self.general_edit and self._shared_view(links)))

    def parent(self, act):
        key = (act["entity"], act["entity_id"])
        if key not in self._parents:
            row = None
            if act["entity"] in PARENTS:
                row = self.conn.execute(f"SELECT * FROM {act['entity']} WHERE id = ?",
                                        (act["entity_id"],)).fetchone()
            self._parents[key] = dict(row) if row else None
        return self._parents[key]

    # ---------------------------------------------------------------- record checks

    def can_view(self, entity, row):
        if self.admin:
            return True
        if entity == "funds":
            return self.can_view_fund(row["id"])
        if entity in FUND_KEYED:
            return self.can_view_fund(row["fund_id"])
        if entity in SHARED:
            return self._shared_view(self.links(entity, row))
        if entity == "activities":
            p = self.parent(row)
            return p is not None and self.can_view(row["entity"], p)
        return False

    def can_edit(self, entity, row):
        if self.admin:
            return True
        if entity == "funds":
            return self.can_edit_fund(row["id"])
        if entity in FUND_KEYED:
            return self.can_edit_fund(row["fund_id"])
        if entity in SHARED:
            return self._shared_edit(self.links(entity, row))
        if entity == "activities":
            p = self.parent(row)
            return p is not None and self.can_edit(row["entity"], p)
        return False

    def can_delete(self, entity, row):
        if self.admin:
            return True
        if entity == "funds":
            return False
        if entity in SHARED:
            links = self.links(entity, row)
            return self._shared_edit(links) and all(self.can_edit_fund(f) for f in links)
        return self.can_edit(entity, row)

    def can_create(self, entity, row):
        """`row` is the full new record (for updates: old values merged with changes)."""
        if self.admin:
            return True
        if entity == "funds":
            return False
        if entity in FUND_KEYED:
            return self.can_edit_fund(row.get("fund_id"))
        if entity == "lps":
            return self.can_create_shared
        if entity == "companies":
            fid = row.get("fund_id")
            return self.can_edit_fund(fid) if fid is not None else self.can_create_shared
        if entity == "activities":
            p = self.parent(row)
            return p is not None and self.can_edit(row["entity"], p)
        return False

    def annotate(self, entity, row):
        row["_can_edit"] = self.can_edit(entity, row)
        row["_can_delete"] = self.can_delete(entity, row)
        return row


def deny(message="You don't have permission to do that."):
    return HTTPException(403, message)
