"""People and their links, document links, tasks, and task reminder emails."""

from vc_crm import jobs
from vc_crm.db import Database

from .conftest import signed_in


def two_funds(client):
    f1 = client.post("/api/funds", json={"name": "Fund I"}).json()
    f2 = client.post("/api/funds", json={"name": "Fund II"}).json()
    lp1 = client.post("/api/lps", json={"name": "LP One"}).json()
    lp2 = client.post("/api/lps", json={"name": "LP Two"}).json()
    client.post("/api/commitments", json={"lp_id": lp1["id"], "fund_id": f1["id"]})
    client.post("/api/commitments", json={"lp_id": lp2["id"], "fund_id": f2["id"]})
    return f1, f2, lp1, lp2


def fund_user(client, app, database_url, fund, level, email="u@firm.com", role="viewer"):
    u = signed_in(app, database_url, email, role)
    uid = u.get("/api/auth/me").json()["id"]
    client.put(f"/api/users/{uid}/funds", json={str(fund["id"]): level})
    return u, uid


# ---------------------------------------------------------------- people

def test_people_link_to_several_organizations(client):
    f1, f2, lp1, lp2 = two_funds(client)
    inv = client.post("/api/investors", json={"name": "VC Firm"}).json()
    pat = client.post("/api/contacts", json={"name": "Pat Lee", "email": "pat@x.com"}).json()
    client.post("/api/contact_links", json={"contact_id": pat["id"], "entity": "lps",
                                            "entity_id": lp1["id"], "role": "CIO",
                                            "is_primary": "Yes"})
    client.post("/api/contact_links", json={"contact_id": pat["id"], "entity": "investors",
                                            "entity_id": inv["id"], "role": "Venture partner"})
    links = client.get(f"/api/contact_links?contact_id={pat['id']}").json()
    assert {(x["entity"], x["role"]) for x in links} == {("lps", "CIO"), ("investors", "Venture partner")}
    assert client.post("/api/contact_links", json={"contact_id": pat["id"], "entity": "lps",
                                                   "entity_id": lp1["id"]}).status_code == 409
    assert client.post("/api/contact_links", json={"contact_id": pat["id"], "entity": "lps",
                                                   "entity_id": 9999}).status_code == 400
    # Linking shows on the LP's history and the person's history.
    assert any(e["entity"] == "contact_links" for e in
               client.get(f"/api/audit?entity=lps&entity_id={lp1['id']}").json())
    assert any(e["entity"] == "contact_links" for e in
               client.get(f"/api/audit?entity=contacts&entity_id={pat['id']}").json())
    # Deleting the LP removes the link, not the person.
    client.delete(f"/api/lps/{lp1['id']}")
    assert [x["entity"] for x in client.get(f"/api/contact_links?contact_id={pat['id']}").json()] == ["investors"]
    assert client.get(f"/api/contacts/{pat['id']}").status_code == 200


def test_people_follow_fund_access(client, app, database_url):
    f1, f2, lp1, lp2 = two_funds(client)
    a = client.post("/api/contacts", json={"name": "Fund I Person"}).json()
    b = client.post("/api/contacts", json={"name": "Fund II Person"}).json()
    c = client.post("/api/contacts", json={"name": "Unlinked Person"}).json()
    client.post("/api/contact_links", json={"contact_id": a["id"], "entity": "lps", "entity_id": lp1["id"]})
    client.post("/api/contact_links", json={"contact_id": b["id"], "entity": "lps", "entity_id": lp2["id"]})
    u, _ = fund_user(client, app, database_url, f2, "editor")
    assert {x["name"] for x in u.get("/api/contacts").json()} == {"Fund II Person", "Unlinked Person"}
    assert u.get(f"/api/contacts/{a['id']}").status_code == 404
    assert u.get("/api/contact_links").json()[0]["contact_id"] == b["id"]
    # Can link people to Fund II records, not Fund I ones.
    assert u.post("/api/contact_links", json={"contact_id": c["id"], "entity": "lps",
                                              "entity_id": lp1["id"]}).status_code == 403
    assert u.post("/api/contact_links", json={"contact_id": c["id"], "entity": "lps",
                                              "entity_id": lp2["id"]}).status_code == 201
    assert ("contacts", "Fund I Person") not in {(h["entity"], h["title"]) for h in
                                                 u.get("/api/search?q=person").json()}


def test_import_people_with_organizations(client):
    f1, f2, lp1, lp2 = two_funds(client)
    text = "Name,Email,LP,Role\nSam Kim,sam@x.com,LP One,Analyst\nNo Org,,LP Nowhere,\n"
    r = client.post("/api/import/contacts", json={"csv": text}).json()
    assert r["ignored_columns"] == [] and r["counts"] == {"ok": 1, "duplicate": 0, "error": 1}
    client.post("/api/import/contacts", json={"csv": text, "commit": True, "skip_errors": True})
    sam = [x for x in client.get("/api/contacts").json() if x["name"] == "Sam Kim"][0]
    link = client.get(f"/api/contact_links?contact_id={sam['id']}").json()[0]
    assert (link["entity"], link["entity_id"], link["role"]) == ("lps", lp1["id"], "Analyst")
    assert "LP One (Analyst)" in client.get("/api/export/contacts.csv").text


# ---------------------------------------------------------------- documents

def test_document_links(client, app, database_url):
    f1, f2, lp1, lp2 = two_funds(client)
    bad = client.post("/api/documents", json={"entity": "lps", "entity_id": lp1["id"],
                                              "title": "x", "url": "javascript:alert(1)"})
    assert bad.status_code == 400
    doc = client.post("/api/documents", json={"entity": "lps", "entity_id": lp1["id"],
                                              "title": "Side letter", "doc_type": "Side letter",
                                              "url": "https://drive.google.com/file/d/abc"})
    assert doc.status_code == 201 and doc.json()["created_by"] is not None
    u, _ = fund_user(client, app, database_url, f2, "editor")
    assert u.get("/api/documents").json() == []
    assert u.post("/api/documents", json={"entity": "lps", "entity_id": lp1["id"], "title": "x",
                                          "url": "https://x.com"}).status_code == 403
    client.delete(f"/api/lps/{lp1['id']}")
    assert client.get("/api/documents").json() == []


# ---------------------------------------------------------------- tasks

def test_tasks_assignment_completion_and_visibility(client, app, database_url, outbox):
    f1, f2, lp1, lp2 = two_funds(client)
    u, uid = fund_user(client, app, database_url, f2, "viewer")
    me = client.get("/api/auth/me").json()
    t = client.post("/api/tasks", json={"title": "Send LPA", "due_date": "2026-01-10",
                                        "assignee_id": uid, "entity": "lps", "entity_id": lp2["id"]}).json()
    assert t["status"] == "Open" and t["created_by"] == me["id"]
    (to, subject, body), = outbox
    assert to == "u@firm.com" and "Send LPA" in subject and "Admin assigned you" in body
    # The assignee can complete it even with view-only fund access.
    done = u.patch(f"/api/tasks/{t['id']}", json={"status": "Done"}).json()
    assert done["completed_at"] is not None
    assert u.patch(f"/api/tasks/{t['id']}", json={"status": "Open"}).json()["completed_at"] is None
    assert u.delete(f"/api/tasks/{t['id']}").status_code == 403  # only creator / record editors
    # A personal task is private to its creator and assignee.
    personal = client.post("/api/tasks", json={"title": "Private"}).json()
    assert u.get(f"/api/tasks/{personal['id']}").status_code == 404
    # Tasks about records the user can't see are hidden, unless assigned to them.
    hidden = client.post("/api/tasks", json={"title": "Fund I", "entity": "lps", "entity_id": lp1["id"]}).json()
    assert u.get(f"/api/tasks/{hidden['id']}").status_code == 404
    assert client.post("/api/tasks", json={"title": "x", "entity": "lps"}).status_code == 400
    assigned = client.post("/api/tasks", json={"title": "Fund I but mine", "assignee_id": uid,
                                               "entity": "lps", "entity_id": lp1["id"]}).json()
    assert u.get(f"/api/tasks/{assigned['id']}").status_code == 200
    # Self-assigned tasks send no email.
    n = len(outbox)
    client.post("/api/tasks", json={"title": "Mine", "assignee_id": me["id"]})
    assert len(outbox) == n
    # Deleting the LP keeps its tasks but unlinks them.
    client.delete(f"/api/lps/{lp2['id']}")
    kept = client.get(f"/api/tasks/{t['id']}").json()
    assert (kept["entity"], kept["entity_id"]) == (None, None)


def test_daily_digest(client, app, database_url):
    a = signed_in(app, database_url, "a@firm.com", "editor")
    b = signed_in(app, database_url, "b@firm.com", "editor")
    aid, bid = a.get("/api/auth/me").json()["id"], b.get("/api/auth/me").json()["id"]
    for title, due, who in [("Old", "2026-01-01", aid), ("Today", "2026-01-05", aid),
                            ("Later", "2026-02-01", aid), ("B today", "2026-01-05", bid)]:
        client.post("/api/tasks", json={"title": title, "due_date": due, "assignee_id": who})
    done = client.post("/api/tasks", json={"title": "Finished", "due_date": "2026-01-01",
                                           "assignee_id": aid}).json()
    client.patch(f"/api/tasks/{done['id']}", json={"status": "Done"})
    b.patch("/api/auth/preferences", json={"email_reminders": False})
    assert b.get("/api/auth/me").json()["email_reminders"] is False
    sent = []
    conn = Database(database_url).connect()
    try:
        n = jobs.send_digests(conn, lambda *m: sent.append(m) or True, "2026-01-05")
        assert jobs.claim_day(conn, "2026-01-05") and not jobs.claim_day(conn, "2026-01-05")
        assert jobs.claim_day(conn, "2026-01-06")
    finally:
        conn.close()
    assert n == 1
    (to, subject, body), = sent
    assert to == "a@firm.com" and subject == "VC CRM: 1 overdue, 1 due today"
    assert "Old (due 2026-01-01)" in body and "Today" in body and "Later" not in body
    assert "Finished" not in body
