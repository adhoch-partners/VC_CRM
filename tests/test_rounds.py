"""Investors directory, portfolio company rounds, and each round's investor pipeline."""

import pytest

from .conftest import signed_in


@pytest.fixture
def portfolio(client):
    post = lambda path, body: client.post(path, json=body).json()  # noqa: E731
    f1 = post("/api/funds", {"name": "Fund I"})
    f2 = post("/api/funds", {"name": "Fund II"})
    co = post("/api/companies", {"name": "Alpha", "fund_id": f1["id"], "deal_stage": "Invested"})
    co2 = post("/api/companies", {"name": "Beta", "fund_id": f2["id"], "deal_stage": "Invested"})
    post("/api/investments", {"company_id": co["id"], "fund_id": f1["id"],
                              "investment_date": "2025-01-01", "amount": 1_000_000})
    post("/api/investments", {"company_id": co2["id"], "fund_id": f2["id"],
                              "investment_date": "2025-01-01", "amount": 1_000_000})
    lead = post("/api/investors", {"name": "Lead Ventures", "investor_type": "VC fund"})
    other = post("/api/investors", {"name": "Angel Group", "investor_type": "Angel group"})
    rnd = post("/api/rounds", {"company_id": co["id"], "round_name": "Series A",
                               "status": "Raising", "target_size": 10_000_000,
                               "lead_investor_id": lead["id"], "pro_rata_amount": 1_500_000,
                               "pro_rata_decision": "Taking full"})
    ri = post("/api/round_investors", {"round_id": rnd["id"], "investor_id": lead["id"],
                                       "stage": "Term sheet", "participation": "Lead",
                                       "amount": 6_000_000, "intro_by": "Our partner"})
    rnd2 = post("/api/rounds", {"company_id": co2["id"], "round_name": "Seed"})
    return dict(f1=f1, f2=f2, co=co, co2=co2, lead=lead, other=other, rnd=rnd, ri=ri, rnd2=rnd2)


def test_round_and_pipeline_round_trip(client, portfolio):
    p = portfolio
    r = client.get(f"/api/rounds/{p['rnd']['id']}").json()
    assert (r["status"], r["target_size"], r["pro_rata_decision"]) == ("Raising", 1e7, "Taking full")
    assert r["_can_edit"] is True
    pipe = client.get(f"/api/round_investors?round_id={p['rnd']['id']}").json()
    assert [(x["investor_id"], x["stage"], x["amount"]) for x in pipe] == [
        (p["lead"]["id"], "Term sheet", 6e6)]
    moved = client.patch(f"/api/round_investors/{p['ri']['id']}", json={"stage": "Committed"})
    assert moved.json()["stage"] == "Committed"
    # One entry per investor per round.
    dup = client.post("/api/round_investors", json={"round_id": p["rnd"]["id"],
                                                    "investor_id": p["lead"]["id"]})
    assert dup.status_code == 409
    assert client.post("/api/round_investors", json={"round_id": p["rnd"]["id"],
                                                     "investor_id": p["lead"]["id"],
                                                     "stage": "Bogus"}).status_code == 400


def test_rounds_only_for_portfolio_companies(client):
    co = client.post("/api/companies", json={"name": "Pipeline Co"}).json()
    r = client.post("/api/rounds", json={"company_id": co["id"], "round_name": "Seed"})
    assert r.status_code == 400 and "portfolio" in r.json()["detail"]


def test_investment_links_to_round_of_same_company(client, portfolio):
    p = portfolio
    ok = client.post("/api/investments", json={
        "company_id": p["co"]["id"], "fund_id": p["f1"]["id"], "investment_date": "2026-01-01",
        "amount": 1_500_000, "round_id": p["rnd"]["id"]})
    assert ok.status_code == 201 and ok.json()["round_id"] == p["rnd"]["id"]
    wrong = client.post("/api/investments", json={
        "company_id": p["co"]["id"], "fund_id": p["f1"]["id"], "investment_date": "2026-01-01",
        "amount": 1, "round_id": p["rnd2"]["id"]})
    assert wrong.status_code == 400 and "different company" in wrong.json()["detail"]
    # Deleting the round keeps the investment, just unlinks it.
    assert client.delete(f"/api/rounds/{p['rnd']['id']}").status_code == 204
    assert client.get(f"/api/investments/{ok.json()['id']}").json()["round_id"] is None


def test_investor_in_use_cannot_be_deleted(client, portfolio):
    p = portfolio
    assert client.delete(f"/api/investors/{p['lead']['id']}").status_code == 409
    assert client.delete(f"/api/investors/{p['other']['id']}").status_code == 204


def test_deleting_company_removes_its_rounds(client, portfolio):
    p = portfolio
    client.post("/api/activities", json={"entity": "rounds", "entity_id": p["rnd"]["id"],
                                         "activity_date": "2026-01-01", "summary": "Board call"})
    assert client.delete(f"/api/companies/{p['co']['id']}").status_code == 204
    assert client.get(f"/api/rounds/{p['rnd']['id']}").status_code == 404
    assert client.get(f"/api/round_investors/{p['ri']['id']}").status_code == 404
    assert client.get("/api/activities?entity=rounds").json() == []


def grant(client, app, database_url, email, role, grants):
    c = signed_in(app, database_url, email, role)
    uid = c.get("/api/auth/me").json()["id"]
    client.put(f"/api/users/{uid}/funds", json={str(k): v for k, v in grants.items()})
    return c


def test_rounds_follow_company_fund_access(client, app, database_url, portfolio):
    p = portfolio
    u = grant(client, app, database_url, "f2@firm.com", "editor", {p["f2"]["id"]: "editor"})
    # Fund II user: sees Beta's round only, never Alpha's round or its pipeline.
    assert [r["company_id"] for r in u.get("/api/rounds").json()] == [p["co2"]["id"]]
    assert u.get(f"/api/rounds/{p['rnd']['id']}").status_code == 404
    assert u.get("/api/round_investors").json() == []
    assert u.post("/api/round_investors", json={"round_id": p["rnd"]["id"],
                                                "investor_id": p["other"]["id"]}).status_code == 403
    assert u.post("/api/rounds", json={"company_id": p["co"]["id"], "round_name": "Seed"}
                  ).status_code == 403
    # ...but can run Beta's raise.
    assert u.post("/api/round_investors", json={"round_id": p["rnd2"]["id"],
                                                "investor_id": p["other"]["id"]}).status_code == 201
    # Moving a pipeline entry into a round they cannot edit is refused.
    entry = u.get("/api/round_investors").json()[0]
    assert u.patch(f"/api/round_investors/{entry['id']}", json={"round_id": p["rnd"]["id"]}
                   ).status_code == 403
    # The investor directory is shared.
    assert {i["name"] for i in u.get("/api/investors").json()} == {"Lead Ventures", "Angel Group"}


def test_view_only_and_general_editor_cannot_change_rounds(client, app, database_url, portfolio):
    p = portfolio
    u = grant(client, app, database_url, "ge@firm.com", "editor", {p["f1"]["id"]: "viewer"})
    r = u.get(f"/api/rounds/{p['rnd']['id']}").json()
    assert r["_can_edit"] is False
    assert u.patch(f"/api/rounds/{p['rnd']['id']}", json={"pro_rata_decision": "Passing"}
                   ).status_code == 403
    assert u.patch(f"/api/round_investors/{p['ri']['id']}", json={"stage": "Passed"}
                   ).status_code == 403
    # General editors still maintain the investor directory.
    assert u.post("/api/investors", json={"name": "New Fund"}).status_code == 201


def test_round_history_and_activity(client, portfolio):
    p = portfolio
    client.patch(f"/api/rounds/{p['rnd']['id']}", json={"status": "Closed", "final_size": 12e6})
    log = client.get(f"/api/audit?entity=rounds&entity_id={p['rnd']['id']}").json()
    assert log[0]["changes"]["status"] == {"from": "Raising", "to": "Closed"}
    act = client.post("/api/activities", json={"entity": "investors", "entity_id": p["lead"]["id"],
                                               "activity_date": "2026-01-01", "summary": "Coffee"})
    assert act.status_code == 201
