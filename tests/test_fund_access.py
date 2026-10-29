"""Per-fund permissions: who sees and changes what across two funds."""

import pytest

from .conftest import signed_in


@pytest.fixture
def world(client):
    """Fund I and Fund II, with LPs/companies linked to one, both, or neither."""
    post = lambda path, body: client.post(path, json=body).json()  # noqa: E731
    f1 = post("/api/funds", {"name": "Fund I"})
    f2 = post("/api/funds", {"name": "Fund II"})
    lp_1 = post("/api/lps", {"name": "Only Fund I LP"})
    lp_2 = post("/api/lps", {"name": "Only Fund II LP"})
    lp_both = post("/api/lps", {"name": "Both funds LP"})
    lp_none = post("/api/lps", {"name": "Unlinked LP"})
    c_1 = post("/api/commitments", {"lp_id": lp_1["id"], "fund_id": f1["id"], "stage": "Closed",
                                    "committed_amount": 1_000_000})
    c_2 = post("/api/commitments", {"lp_id": lp_2["id"], "fund_id": f2["id"], "stage": "Closed",
                                    "committed_amount": 2_000_000})
    cb_1 = post("/api/commitments", {"lp_id": lp_both["id"], "fund_id": f1["id"],
                                     "stage": "Closed", "committed_amount": 3_000_000})
    cb_2 = post("/api/commitments", {"lp_id": lp_both["id"], "fund_id": f2["id"],
                                     "stage": "Meeting", "target_amount": 4_000_000})
    co_1 = post("/api/companies", {"name": "Fund I Co", "fund_id": f1["id"]})
    co_2 = post("/api/companies", {"name": "Fund II Co", "fund_id": f2["id"]})
    co_none = post("/api/companies", {"name": "Unassigned Co"})
    # Fund I also invested in the "Fund II Co" target: now linked to both.
    co_x = post("/api/companies", {"name": "Co-invested Co", "fund_id": f2["id"]})
    post("/api/investments", {"company_id": co_1["id"], "fund_id": f1["id"],
                              "investment_date": "2025-01-01", "amount": 500_000})
    post("/api/investments", {"company_id": co_2["id"], "fund_id": f2["id"],
                              "investment_date": "2025-01-01", "amount": 700_000})
    post("/api/investments", {"company_id": co_x["id"], "fund_id": f1["id"],
                              "investment_date": "2025-01-01", "amount": 900_000})
    post("/api/capital-events", {"fund_id": f1["id"], "event_type": "Capital call",
                                 "event_date": "2025-01-01", "total_amount": 400_000})
    post("/api/activities", {"entity": "commitments", "entity_id": c_1["id"],
                             "activity_date": "2025-01-01", "summary": "Fund I secret"})
    return dict(f1=f1, f2=f2, lp_1=lp_1, lp_2=lp_2, lp_both=lp_both, lp_none=lp_none,
                c_1=c_1, c_2=c_2, cb_1=cb_1, cb_2=cb_2, co_1=co_1, co_2=co_2,
                co_none=co_none, co_x=co_x)


def user(client, app, database_url, email, role, grants):
    c = signed_in(app, database_url, email, role)
    uid = c.get("/api/auth/me").json()["id"]
    r = client.put(f"/api/users/{uid}/funds", json={str(k): v for k, v in grants.items()})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def f2_editor(client, app, database_url, world):
    """General role viewer, editor on Fund II only."""
    return user(client, app, database_url, "f2@firm.com", "viewer", {world["f2"]["id"]: "editor"})


names = lambda rows: sorted(r["name"] for r in rows)  # noqa: E731


def test_new_user_and_new_fund_default_to_no_access(client, app, database_url, world):
    u = signed_in(app, database_url, "new@firm.com", "editor")
    assert u.get("/api/funds").json() == []
    assert names(u.get("/api/lps").json()) == ["Unlinked LP"]
    assert names(u.get("/api/companies").json()) == ["Unassigned Co"]
    assert u.get("/api/commitments").json() == []
    assert u.get("/api/capital-events").json() == []
    assert u.get("/api/portfolio").json()["holdings"] == []
    me = u.get("/api/auth/me").json()
    assert me["fund_access"] == {} and me["admin"] is False


def test_visibility_is_limited_to_granted_funds(f2_editor, world):
    u = f2_editor
    assert names(u.get("/api/funds").json()) == ["Fund II"]
    assert names(u.get("/api/lps").json()) == ["Both funds LP", "Only Fund II LP", "Unlinked LP"]
    # The shared LP shows only its Fund II commitment, not the Fund I amount.
    commits = u.get(f"/api/commitments?lp_id={world['lp_both']['id']}").json()
    assert [c["fund_id"] for c in commits] == [world["f2"]["id"]]
    assert names(u.get("/api/companies").json()) == ["Co-invested Co", "Fund II Co",
                                                     "Unassigned Co"]
    # Fund I's investment in the co-invested company stays hidden.
    inv = u.get(f"/api/investments?company_id={world['co_x']['id']}").json()
    assert inv == []
    # Direct reads of hidden records look like they do not exist.
    for path in [f"/api/funds/{world['f1']['id']}", f"/api/lps/{world['lp_1']['id']}",
                 f"/api/commitments/{world['c_1']['id']}", f"/api/companies/{world['co_1']['id']}",
                 f"/api/portfolio?fund_id={world['f1']['id']}",
                 f"/api/lp-positions?fund_id={world['f1']['id']}",
                 f"/api/audit?entity=lps&entity_id={world['lp_1']['id']}"]:
        assert u.get(path).status_code == 404, path
    assert u.get("/api/activities").json() == []  # Fund I commitment's note
    assert u.get("/api/capital-events").json() == []


def test_analytics_only_cover_visible_funds(f2_editor, client):
    d = f2_editor.get("/api/dashboard").json()
    assert [f["name"] for f in d["funds"]] == ["Fund II"]
    assert d["portfolio"]["invested"] == 700_000  # Fund I's 1.4M excluded
    assert d["deal_pipeline"]["Sourced"] == 3  # Fund II Co, Co-invested Co, Unassigned Co
    assert [p["lp"] for p in f2_editor.get("/api/lp-positions").json()] == ["Only Fund II LP"]
    assert [f["name"] for f in f2_editor.get("/api/funds/summary").json()] == ["Fund II"]
    assert client.get("/api/dashboard").json()["portfolio"]["invested"] == 2_100_000


def test_editor_on_fund_can_change_that_fund_only(f2_editor, world):
    u, f1, f2 = f2_editor, world["f1"]["id"], world["f2"]["id"]
    assert u.patch(f"/api/commitments/{world['c_2']['id']}", json={"next_step": "Send docs"}
                   ).status_code == 200
    assert u.patch(f"/api/funds/{f2}", json={"target_size": 1e8}).status_code == 200
    # Fund-level writes elsewhere are refused.
    assert u.post("/api/commitments", json={"lp_id": world["lp_none"]["id"], "fund_id": f1}
                  ).status_code == 403
    assert u.post("/api/investments", json={"company_id": world["co_none"]["id"], "fund_id": f1,
                                            "investment_date": "2025-01-01", "amount": 1}
                  ).status_code == 403
    # Moving a Fund II commitment into Fund I is refused.
    assert u.patch(f"/api/commitments/{world['c_2']['id']}", json={"fund_id": f1}
                   ).status_code == 403
    # Only admins create or delete funds.
    assert u.post("/api/funds", json={"name": "Fund III"}).status_code == 403
    assert u.delete(f"/api/funds/{f2}").status_code == 403
    # Capital events in their fund work.
    assert u.post("/api/capital-events", json={"fund_id": f2, "event_type": "Capital call",
                                               "event_date": "2025-06-01", "total_amount": 1000}
                  ).status_code == 201


def test_shared_records_follow_linked_funds(f2_editor, world):
    u = f2_editor
    # Linked to Fund II (editor): editable, even though the general role is viewer.
    lp_both = u.get(f"/api/lps/{world['lp_both']['id']}").json()
    assert lp_both["_can_edit"] is True
    assert u.patch(f"/api/lps/{world['lp_both']['id']}", json={"location": "SF"}).status_code == 200
    # ...but deleting it would delete the Fund I commitment, so it is refused.
    assert lp_both["_can_delete"] is False
    r = u.delete(f"/api/lps/{world['lp_both']['id']}")
    assert r.status_code == 403 and "cannot edit" in r.json()["detail"]
    # Only linked to Fund II: deletable.
    assert u.delete(f"/api/lps/{world['lp_2']['id']}").status_code == 204
    # Unlinked records: anyone who can edit some fund can work the pipeline.
    assert u.patch(f"/api/companies/{world['co_none']['id']}", json={"deal_stage": "Diligence"}
                   ).status_code == 200
    assert u.post("/api/lps", json={"name": "New prospect"}).status_code == 201
    # Assigning an unassigned deal to a fund needs edit rights on that fund.
    assert u.patch(f"/api/companies/{world['co_none']['id']}",
                   json={"fund_id": world["f1"]["id"]}).status_code == 403
    assert u.patch(f"/api/companies/{world['co_none']['id']}",
                   json={"fund_id": world["f2"]["id"]}).status_code == 200
    # The co-invested company: may edit (Fund II), may not delete (Fund I position).
    assert u.delete(f"/api/companies/{world['co_x']['id']}").status_code == 403


def test_fund_viewer_is_read_only_for_that_fund(client, app, database_url, world):
    u = user(client, app, database_url, "v@firm.com", "viewer", {world["f1"]["id"]: "viewer"})
    assert names(u.get("/api/funds").json()) == ["Fund I"]
    c = u.get(f"/api/commitments/{world['c_1']['id']}").json()
    assert c["_can_edit"] is False and c["_can_delete"] is False
    assert u.patch(f"/api/commitments/{world['c_1']['id']}", json={"stage": "Declined"}
                   ).status_code == 403
    assert u.patch(f"/api/lps/{world['lp_1']['id']}", json={"location": "x"}).status_code == 403
    assert u.post("/api/lps", json={"name": "x"}).status_code == 403  # no edit anywhere
    acts = u.get("/api/activities").json()
    assert [a["summary"] for a in acts] == ["Fund I secret"] and acts[0]["_can_edit"] is False
    assert u.post("/api/activities", json={"entity": "commitments", "entity_id": world["c_1"]["id"],
                                           "activity_date": "2025-01-02", "summary": "x"}
                  ).status_code == 403
    assert u.get(f"/api/audit?entity=commitments&entity_id={world['c_1']['id']}").status_code == 200
    assert u.get("/api/audit").status_code == 403


def test_general_editor_edits_visible_shared_records(client, app, database_url, world):
    """General role editor + view on Fund I: may edit the LP's contact details, not its money."""
    u = user(client, app, database_url, "ge@firm.com", "editor", {world["f1"]["id"]: "viewer"})
    assert u.patch(f"/api/lps/{world['lp_1']['id']}", json={"location": "Boston"}
                   ).status_code == 200
    assert u.patch(f"/api/commitments/{world['c_1']['id']}", json={"committed_amount": 1}
                   ).status_code == 403
    assert u.delete(f"/api/lps/{world['lp_1']['id']}").status_code == 403


def test_activities_need_parent_access(f2_editor, world):
    u = f2_editor
    ok = {"entity": "commitments", "entity_id": world["c_2"]["id"],
          "activity_date": "2025-01-01", "summary": "Call"}
    assert u.post("/api/activities", json=ok).status_code == 201
    hidden = {**ok, "entity_id": world["c_1"]["id"]}
    assert u.post("/api/activities", json=hidden).status_code == 403
    missing = {**ok, "entity_id": 99999}
    assert u.post("/api/activities", json=missing).status_code == 400
    act = u.get("/api/activities").json()[0]
    # Re-pointing an activity at a record in another fund is refused.
    assert u.patch(f"/api/activities/{act['id']}", json={"entity_id": world["c_1"]["id"]}
                   ).status_code == 403


def test_access_changes_apply_immediately(client, f2_editor, world):
    uid = f2_editor.get("/api/auth/me").json()["id"]
    client.put(f"/api/users/{uid}/funds", json={str(world["f2"]["id"]): "viewer"})
    assert f2_editor.patch(f"/api/commitments/{world['c_2']['id']}", json={"stage": "Declined"}
                           ).status_code == 403
    client.put(f"/api/users/{uid}/funds", json={})
    assert f2_editor.get("/api/funds").json() == []


def test_fund_access_admin_api(client, f2_editor, world):
    uid = f2_editor.get("/api/auth/me").json()["id"]
    assert client.get(f"/api/users/{uid}/funds").json() == {str(world["f2"]["id"]): "editor"}
    assert client.put(f"/api/users/{uid}/funds", json={"1": "owner"}).status_code == 400
    assert client.put(f"/api/users/{uid}/funds", json={"99999": "viewer"}).status_code == 400
    r = client.put(f"/api/users/{uid}/funds", json={str(world["f1"]["id"]): "viewer",
                                                    str(world["f2"]["id"]): "none"})
    assert r.json() == {str(world["f1"]["id"]): "viewer"}
    assert f2_editor.get(f"/api/users/{uid}/funds").status_code == 403
    log = client.get(f"/api/audit?entity=users&entity_id={uid}").json()
    assert log[0]["changes"] == {"fund_access": {str(world["f1"]["id"]): "viewer"}}


def test_admin_sees_everything(client, world):
    assert len(client.get("/api/funds").json()) == 2
    assert len(client.get("/api/lps").json()) == 4
    assert all(r["_can_edit"] and r["_can_delete"] for r in client.get("/api/lps").json())
    assert client.get("/api/auth/me").json()["admin"] is True


def test_deleted_fund_removes_its_grants(client, app, database_url):
    f = client.post("/api/funds", json={"name": "Temp"}).json()
    u = user(client, app, database_url, "t@firm.com", "viewer", {f["id"]: "editor"})
    assert client.delete(f"/api/funds/{f['id']}").status_code == 204
    assert u.get("/api/auth/me").json()["fund_access"] == {}
