"""Change history on a record includes its child records' changes, filtered by access."""

from .conftest import signed_in


def history(c, entity, rid, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = c.get(f"/api/audit?entity={entity}&entity_id={rid}&{q}")
    assert r.status_code == 200, r.text
    return r.json()


def summary(entries):
    return [(e["action"], e["entity"]) for e in entries]


def test_lp_history_includes_commitments_and_activities(client):
    f = client.post("/api/funds", json={"name": "Fund I"}).json()
    lp = client.post("/api/lps", json={"name": "LP"}).json()
    c = client.post("/api/commitments", json={"lp_id": lp["id"], "fund_id": f["id"]}).json()
    client.patch(f"/api/commitments/{c['id']}", json={"stage": "Meeting", "target_amount": 5e6})
    client.post("/api/activities", json={"entity": "lps", "entity_id": lp["id"],
                                         "activity_date": "2026-01-01", "summary": "Call"})
    client.patch(f"/api/lps/{lp['id']}", json={"location": "NYC"})
    log = history(client, "lps", lp["id"])
    assert summary(log) == [("update", "lps"), ("create", "activities"),
                            ("update", "commitments"), ("create", "commitments"), ("create", "lps")]
    upd = log[2]
    assert upd["changes"] == {"stage": {"from": "Prospect", "to": "Meeting"},
                              "target_amount": {"from": None, "to": 5e6}}
    # The snapshot lets the page label the child ("LP / Fund I") without another lookup.
    assert upd["record"]["lp_id"] == lp["id"] and upd["record"]["fund_id"] == f["id"]
    # The fund shows the same commitment changes.
    assert summary(history(client, "funds", f["id"]))[:2] == [("update", "commitments"),
                                                              ("create", "commitments")]
    # related=false keeps only the record's own changes.
    assert summary(history(client, "lps", lp["id"], related="false")) == [
        ("update", "lps"), ("create", "lps")]


def test_deleted_and_moved_children_stay_in_history(client):
    f = client.post("/api/funds", json={"name": "Fund I"}).json()
    a = client.post("/api/lps", json={"name": "A"}).json()
    b = client.post("/api/lps", json={"name": "B"}).json()
    c = client.post("/api/commitments", json={"lp_id": a["id"], "fund_id": f["id"]}).json()
    client.patch(f"/api/commitments/{c['id']}", json={"lp_id": b["id"]})
    # The move shows on both the old and the new LP.
    assert ("update", "commitments") in summary(history(client, "lps", a["id"]))
    assert ("update", "commitments") in summary(history(client, "lps", b["id"]))
    client.delete(f"/api/commitments/{c['id']}")
    log = history(client, "lps", b["id"])
    assert log[0]["action"] == "delete" and log[0]["record"]["id"] == c["id"]


def test_company_round_and_investor_histories(client):
    f = client.post("/api/funds", json={"name": "Fund I"}).json()
    co = client.post("/api/companies", json={"name": "Co", "fund_id": f["id"]}).json()
    client.post("/api/investments", json={"company_id": co["id"], "fund_id": f["id"],
                                          "investment_date": "2025-01-01", "amount": 1e6})
    client.post("/api/valuations", json={"company_id": co["id"], "fund_id": f["id"],
                                         "valuation_date": "2025-06-01", "fair_value": 2e6})
    rnd = client.post("/api/rounds", json={"company_id": co["id"], "round_name": "Series A"}).json()
    inv = client.post("/api/investors", json={"name": "VC"}).json()
    ri = client.post("/api/round_investors", json={"round_id": rnd["id"],
                                                   "investor_id": inv["id"]}).json()
    client.patch(f"/api/round_investors/{ri['id']}", json={"stage": "Committed", "amount": 3e6})
    assert {"investments", "valuations", "rounds"} <= {e["entity"] for e in
                                                       history(client, "companies", co["id"])}
    assert summary(history(client, "rounds", rnd["id"]))[:2] == [("update", "round_investors"),
                                                                 ("create", "round_investors")]
    assert summary(history(client, "investors", inv["id"]))[0] == ("update", "round_investors")


def test_capital_event_payment_history(client):
    f = client.post("/api/funds", json={"name": "Fund I"}).json()
    lp = client.post("/api/lps", json={"name": "Paying LP"}).json()
    client.post("/api/commitments", json={"lp_id": lp["id"], "fund_id": f["id"],
                                          "stage": "Closed", "committed_amount": 100})
    ev = client.post("/api/capital-events", json={"fund_id": f["id"], "event_type": "Capital call",
                                                  "event_date": "2026-01-01",
                                                  "total_amount": 10}).json()
    alloc = client.get(f"/api/capital-events/{ev['id']}/allocations").json()[0]
    client.patch(f"/api/allocations/{alloc['id']}", json={"paid": True})
    log = history(client, "capital_events", ev["id"])
    assert log[0]["changes"] == {"paid": {"from": 0, "to": 1}, "lp": "Paying LP"}
    assert summary(history(client, "funds", f["id"]))[:2] == [("update", "capital_events"),
                                                              ("create", "capital_events")]


def test_history_hides_changes_in_funds_you_cannot_see(client, app, database_url):
    f1 = client.post("/api/funds", json={"name": "Fund I"}).json()
    f2 = client.post("/api/funds", json={"name": "Fund II"}).json()
    lp = client.post("/api/lps", json={"name": "Shared LP"}).json()
    c1 = client.post("/api/commitments", json={"lp_id": lp["id"], "fund_id": f1["id"],
                                               "target_amount": 9e6}).json()
    c2 = client.post("/api/commitments", json={"lp_id": lp["id"], "fund_id": f2["id"]}).json()
    client.patch(f"/api/commitments/{c1['id']}", json={"stage": "Meeting"})
    u = signed_in(app, database_url, "f2@firm.com", "viewer")
    uid = u.get("/api/auth/me").json()["id"]
    client.put(f"/api/users/{uid}/funds", json={str(f2["id"]): "viewer"})
    log = history(u, "lps", lp["id"])
    assert {e["record"]["fund_id"] for e in log if e["entity"] == "commitments"} == {f2["id"]}
    assert all(e["entity"] != "users" for e in log)
    assert u.get(f"/api/audit?entity=funds&entity_id={f1['id']}").status_code == 404
    # An admin sees both.
    assert {e["record"]["id"] for e in history(client, "lps", lp["id"])
            if e["entity"] == "commitments"} == {c1["id"], c2["id"]}


def test_history_pages_with_before(client):
    lp = client.post("/api/lps", json={"name": "LP"}).json()
    for i in range(7):
        client.patch(f"/api/lps/{lp['id']}", json={"location": f"City {i}"})
    first = history(client, "lps", lp["id"], limit=5)
    rest = history(client, "lps", lp["id"], limit=5, before=first[-1]["id"])
    assert len(first) == 5 and len(rest) == 3  # 7 updates + the create
    assert rest[-1]["action"] == "create"
    assert not {e["id"] for e in first} & {e["id"] for e in rest}
