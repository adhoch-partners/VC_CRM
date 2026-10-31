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

Investors (outside funds and angels) are a directory everyone can see; anyone
who can create shared records can maintain it.

A portfolio company's rounds, and each round's investor pipeline, follow the
company: visible to whoever can see the company, changeable only by editors
on a fund linked to it (the general role is not enough: rounds carry our
pro-rata decisions).

People (contacts) are visible when linked to something the user can see, or when
not linked to anything yet. Activity notes, document links and person links follow
the record they are attached to. Tasks are visible to their assignee and creator, and
to anyone who can see the record they relate to; a task with no record is personal.

The user's general role (editor / viewer) only governs shared records.
"""

from fastapi import HTTPException

LEVELS = ["viewer", "editor"]
FUND_KEYED = {"commitments", "investments", "valuations", "realizations", "capital_events"}
SHARED = {"lps", "companies"}
ROUND_SCOPED = {"rounds", "round_investors"}
# Records that activities, documents, tasks and person links can be attached to.
PARENTS = {"lps", "commitments", "companies", "funds", "investors", "rounds", "capital_events"}
ATTACHED = {"activities", "documents", "contact_links"}  # always need a parent record


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

    def _row(self, table, rid):
        key = (table, rid)
        if key not in self._parents:
            row = self.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
            self._parents[key] = dict(row) if row else None
        return self._parents[key]

    def parent(self, act):
        if act.get("entity") not in PARENTS or act.get("entity_id") is None:
            return None
        return self._row(act["entity"], act["entity_id"])

    def _contact_parents(self, contact_id):
        """(entity, row) for every record a person is linked to (rows that still exist)."""
        out = []
        for link in self.conn.execute("SELECT entity, entity_id FROM contact_links "
                                      "WHERE contact_id = ?", (contact_id,)):
            row = self._row(link["entity"], link["entity_id"])
            if row is not None:
                out.append((link["entity"], row))
        return out

    def _task_mine(self, row):
        return self.user["id"] in (row.get("assignee_id"), row.get("created_by"))

    def _round_company(self, entity, row):
        """The company a round (or a round pipeline entry) belongs to."""
        if entity == "round_investors":
            row = self._row("rounds", row.get("round_id"))
            if row is None:
                return None
        return self._row("companies", row.get("company_id"))

    def _company_editor(self, company):
        return company is not None and any(
            self.can_edit_fund(f) for f in self.links("companies", company))

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
        if entity == "investors":
            return True
        if entity in ROUND_SCOPED:
            company = self._round_company(entity, row)
            return company is not None and self.can_view("companies", company)
        if entity in ATTACHED:
            p = self.parent(row)
            return p is not None and self.can_view(row["entity"], p)
        if entity == "contacts":
            parents = self._contact_parents(row["id"])
            return not parents or any(self.can_view(e, r) for e, r in parents)
        if entity == "tasks":
            p = self.parent(row)
            return self._task_mine(row) or (p is not None and self.can_view(row["entity"], p))
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
        if entity == "investors":
            return self.can_create_shared
        if entity in ROUND_SCOPED:
            return self._company_editor(self._round_company(entity, row))
        if entity in ATTACHED:
            p = self.parent(row)
            return p is not None and self.can_edit(row["entity"], p)
        if entity == "contacts":
            parents = self._contact_parents(row["id"])
            if not parents:
                return self.can_create_shared
            return any(self.can_edit(e, r) for e, r in parents)
        if entity == "tasks":
            p = self.parent(row)
            return self._task_mine(row) or (p is not None and self.can_edit(row["entity"], p))
        return False

    def can_delete(self, entity, row):
        if self.admin:
            return True
        if entity == "funds":
            return False
        if entity in SHARED:
            links = self.links(entity, row)
            return self._shared_edit(links) and all(self.can_edit_fund(f) for f in links)
        if entity == "contacts":
            # Deleting a person removes them from every record they are linked to.
            parents = self._contact_parents(row["id"])
            return self.can_edit(entity, row) and all(self.can_edit(e, r) for e, r in parents)
        if entity == "tasks":
            p = self.parent(row)
            return (row.get("created_by") == self.user["id"]
                    or (p is not None and self.can_edit(row["entity"], p)))
        return self.can_edit(entity, row)

    def can_create(self, entity, row):
        """`row` is the full new record (for updates: old values merged with changes)."""
        if self.admin:
            return True
        if entity == "funds":
            return False
        if entity in FUND_KEYED:
            return self.can_edit_fund(row.get("fund_id"))
        if entity in ("lps", "investors"):
            return self.can_create_shared
        if entity in ROUND_SCOPED:
            return self._company_editor(self._round_company(entity, row))
        if entity == "companies":
            fid = row.get("fund_id")
            return self.can_edit_fund(fid) if fid is not None else self.can_create_shared
        if entity in ATTACHED:
            p = self.parent(row)
            return p is not None and self.can_edit(row["entity"], p)
        if entity == "contacts":
            return self.can_create_shared
        if entity == "tasks":
            # Anyone may track a to-do about a record they can see.
            if row.get("entity") is None:
                return True
            p = self.parent(row)
            return p is not None and self.can_view(row["entity"], p)
        return False

    def annotate(self, entity, row):
        row["_can_edit"] = self.can_edit(entity, row)
        row["_can_delete"] = self.can_delete(entity, row)
        return row


def deny(message="You don't have permission to do that."):
    return HTTPException(403, message)
