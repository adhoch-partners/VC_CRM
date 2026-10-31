"""Portfolio KPIs, custom fields, saved views and pipeline analytics."""

import pytest

from .conftest import signed_in


def user_on(client, app, database_url, fund, level, email="u@firm.com", role="viewer"):
    u = signed_in(app, database_url, email, role)
    uid = u.get("/api/auth/me").json()["id"]
    if fund:
        client.put(f"/api/users/{uid}/funds", json={str(fund["id"]): level})
    return u


@pytest.fixture
def portfolio_co(client):
    f = client.post("/api/funds", json={"name": "Fund I"}).json()
    co = client.post("/api/companies", json={"name": "Acme", "fund_id": f["id"],
                                             "deal_stage": "Invested"}).json()
    client.post("/api/investments", json={"company_id": co["id"], "fund_id": f["id"],
                                          "investment_date": "2025-01-01", "amount": 1e6})
    return f, co


# ---------------------------------------------------------------- KPIs

def test_company_kpis(client, app, database_url, portfolio_co):
    f, co = portfolio_co
    k = client.post("/api/company_metrics", json={"company_id": co["id"], "period_end": "2026-06-30",
                                                  "arr": 2_400_000, "burn": 150_000, "cash": 3_000_000,
                                                  "headcount": 24, "gross_margin": 72})
    assert k.status_code == 201
    assert client.post("/api/company_metrics", json={"company_id": co["id"],
                                                     "period_end": "2026-06-30"}).status_code == 409
    assert client.get(f"/api/company_metrics?company_id={co['id']}").json()[0]["arr"] == 2_400_000
    assert any(e["entity"] == "company_metrics" for e in
               client.get(f"/api/audit?entity=companies&entity_id={co['id']}").json())
    viewer = user_on(client, app, database_url, f, "viewer")
    assert len(viewer.get("/api/company_metrics").json()) == 1
    assert viewer.post("/api/company_metrics", json={"company_id": co["id"],
                                                     "period_end": "2026-09-30"}).status_code == 403
    outsider = user_on(client, app, database_url, None, None, email="o@firm.com", role="editor")
    assert outsider.get("/api/company_metrics").json() == []


# ---------------------------------------------------------------- custom fields

def test_custom_fields_end_to_end(client, app, database_url):
    r = client.post("/api/custom-fields", json={"entity": "lps", "label": "Tier",
                                                "field_type": "select", "options": "A, B, C"})
    assert r.status_code == 201
    tier = r.json()["name"]
    assert tier == "cf_tier"
    aum = client.post("/api/custom-fields", json={"entity": "lps", "label": "AUM",
                                                  "field_type": "money"}).json()["name"]
    meta_fields = {f["name"]: f for f in client.get("/api/meta").json()["entities"]["lps"]["fields"]}
    assert meta_fields[tier]["options"] == ["A", "B", "C"] and meta_fields[aum]["custom"] is True
    lp = client.post("/api/lps", json={"name": "LP", tier: "A", aum: "2,000,000,000"}).json()
    assert (lp[tier], lp[aum]) == ("A", 2e9) and "custom" not in lp
    assert client.post("/api/lps", json={"name": "Bad", tier: "Z"}).status_code == 400
    upd = client.patch(f"/api/lps/{lp['id']}", json={tier: "B"}).json()
    assert upd[tier] == "B" and upd[aum] == 2e9  # other custom values kept
    log = client.get(f"/api/audit?entity=lps&entity_id={lp['id']}").json()
    assert log[0]["changes"] == {tier: {"from": "A", "to": "B"}}
    assert client.get("/api/lps").json()[0][tier] == "B"
    # Export and import use the label.
    assert "Tier" in client.get("/api/export/lps.csv").text.splitlines()[0]
    client.post("/api/import/lps", json={"csv": "LP name,Tier\nImported LP,c\n", "commit": True})
    imported = [x for x in client.get("/api/lps").json() if x["name"] == "Imported LP"][0]
    assert imported[tier] == "C"
    # Only admins define fields; removing one hides it but keeps the data.
    u = signed_in(app, database_url, "e@firm.com", "editor")
    assert u.post("/api/custom-fields", json={"entity": "lps", "label": "X"}).status_code == 403
    fid = [x for x in client.get("/api/custom-fields").json() if x["key"] == "tier"][0]["id"]
    assert client.patch(f"/api/custom-fields/{fid}", json={"field_type": "text"}).status_code == 400
    client.delete(f"/api/custom-fields/{fid}")
    assert tier not in {f["name"] for f in client.get("/api/meta").json()["entities"]["lps"]["fields"]}
    assert client.patch(f"/api/lps/{lp['id']}", json={tier: "A"}).status_code == 400
    client.post("/api/custom-fields", json={"entity": "lps", "label": "Tier",
                                            "field_type": "select", "options": ["A", "B", "C"]})
    assert client.get(f"/api/lps/{lp['id']}").json()[tier] == "B"


def test_custom_field_validation(client):
    assert client.post("/api/custom-fields", json={"entity": "users", "label": "X"}).status_code == 400
    assert client.post("/api/custom-fields", json={"entity": "lps", "label": "X",
                                                   "field_type": "select"}).status_code == 400
    assert client.post("/api/custom-fields", json={"entity": "lps", "label": "!!!"}).status_code == 400
    # A label that collides with a built-in field gets a distinct key.
    f = client.post("/api/custom-fields", json={"entity": "lps", "label": "Name"}).json()
    assert f["name"] == "cf_name"
    g = client.post("/api/custom-fields", json={"entity": "lps", "label": "name"}).json()
    assert g["name"] == "cf_name_2"


# ---------------------------------------------------------------- saved views

def test_saved_views(client, app, database_url):
    u = signed_in(app, database_url, "u@firm.com", "viewer")
    mine = client.post("/api/saved-views", json={"view": "lps", "name": "Endowments",
                                                 "state": {"filters": [["lp_type", "Endowment"]]}}).json()
    client.post("/api/saved-views", json={"view": "lps", "name": "Team view", "shared": True,
                                          "state": {"q": "boston"}})
    assert {v["name"] for v in client.get("/api/saved-views?view=lps").json()} == {"Endowments", "Team view"}
    theirs = u.get("/api/saved-views?view=lps").json()
    assert [v["name"] for v in theirs] == ["Team view"] and theirs[0]["mine"] is False
    assert u.delete(f"/api/saved-views/{theirs[0]['id']}").status_code == 403
    # Same name replaces.
    client.post("/api/saved-views", json={"view": "lps", "name": "Endowments", "state": {"q": "x"}})
    again = [v for v in client.get("/api/saved-views?view=lps").json() if v["name"] == "Endowments"]
    assert len(again) == 1 and again[0]["state"] == {"q": "x"}
    assert client.delete(f"/api/saved-views/{again[0]['id']}").status_code == 204
    assert client.post("/api/saved-views", json={"view": "lps", "name": ""}).status_code == 400


# ---------------------------------------------------------------- analytics

def test_lp_pipeline_analytics(client, app, database_url):
    f1 = client.post("/api/funds", json={"name": "Fund I"}).json()
    f2 = client.post("/api/funds", json={"name": "Fund II"}).json()
    ids = []
    for i in range(4):
        lp = client.post("/api/lps", json={"name": f"LP {i}"}).json()
        ids.append(client.post("/api/commitments", json={"lp_id": lp["id"], "fund_id": f1["id"]}).json()["id"])
    path = ["Contacted", "Meeting", "Due diligence", "Soft circle", "Legal / docs", "Closed"]
    for s in path:  # LP 0 closes
        client.patch(f"/api/commitments/{ids[0]}", json={"stage": s, "committed_amount": 1})
    for s in ["Contacted", "Meeting"]:  # LP 1 reaches Meeting, then declines
        client.patch(f"/api/commitments/{ids[1]}", json={"stage": s})
    client.patch(f"/api/commitments/{ids[1]}", json={"stage": "Declined"})
    client.patch(f"/api/commitments/{ids[2]}", json={"stage": "Contacted"})  # LP 2 in progress
    other_lp = client.post("/api/lps", json={"name": "Other"}).json()
    client.post("/api/commitments", json={"lp_id": other_lp["id"], "fund_id": f2["id"]})

    a = client.get(f"/api/analytics/pipeline?kind=lp&fund_id={f1['id']}").json()
    st = {s["stage"]: s for s in a["stages"]}
    assert a["total"] == 4 and a["won"] == 1 and a["lost"] == 1 and a["win_rate"] == 0.5
    assert [st[s]["reached"] for s in ["Prospect", "Contacted", "Meeting", "Closed"]] == [4, 3, 2, 1]
    assert st["Contacted"]["conversion_to_next"] == pytest.approx(2 / 3)
    assert st["Meeting"]["lost_here"] == 1
    assert st["Prospect"]["current"] == 1 and st["Contacted"]["current"] == 1
    assert st["Prospect"]["median_days"] is not None  # three completed stays
    assert "Declined" not in st
    assert client.get("/api/analytics/pipeline?kind=lp").json()["total"] == 5
    u = user_on(client, app, database_url, f2, "viewer")
    assert u.get("/api/analytics/pipeline?kind=lp").json()["total"] == 1
    assert u.get(f"/api/analytics/pipeline?kind=lp&fund_id={f1['id']}").status_code == 404
    assert client.get("/api/analytics/pipeline?kind=x").status_code == 400


def test_deal_pipeline_analytics(client):
    a = client.post("/api/companies", json={"name": "A"}).json()
    b = client.post("/api/companies", json={"name": "B"}).json()
    client.patch(f"/api/companies/{a['id']}", json={"deal_stage": "First meeting"})
    client.patch(f"/api/companies/{a['id']}", json={"deal_stage": "Passed", "pass_reason": "Too early"})
    client.patch(f"/api/companies/{b['id']}", json={"deal_stage": "Passed"})
    r = client.get("/api/analytics/pipeline?kind=deal").json()
    assert r["lost"] == 2 and r["won"] == 0 and r["win_rate"] == 0
    assert dict(r["pass_reasons"]) == {"Too early": 1, "No reason given": 1}
