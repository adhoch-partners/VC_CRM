import pytest
from fastapi.testclient import TestClient

from vc_crm.app import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "test.db")))


def post(client, path, body, status=201):
    r = client.post(path, json=body)
    assert r.status_code == status, r.text
    return r.json()


@pytest.fixture
def fund_with_lps(client):
    fund = post(client, "/api/funds", {"name": "Fund I", "vintage": 2025, "target_size": 50_000_000})
    a = post(client, "/api/lps", {"name": "LP A", "lp_type": "Endowment"})
    b = post(client, "/api/lps", {"name": "LP B", "lp_type": "Family office"})
    ca = post(client, "/api/commitments", {"lp_id": a["id"], "fund_id": fund["id"], "stage": "Closed",
                                           "committed_amount": 7_500_000})
    cb = post(client, "/api/commitments", {"lp_id": b["id"], "fund_id": fund["id"], "stage": "Closed",
                                           "committed_amount": "2,500,000"})
    return fund, (a, b), (ca, cb)


# ---------------------------------------------------------------- CRUD / validation

def test_crud_round_trip(client):
    lp = post(client, "/api/lps", {"name": "  Acme Endowment ", "lp_type": "Endowment"})
    assert lp["name"] == "Acme Endowment"
    r = client.patch(f"/api/lps/{lp['id']}", json={"location": "Boston"})
    assert r.json()["location"] == "Boston"
    assert [x["name"] for x in client.get("/api/lps").json()] == ["Acme Endowment"]
    assert client.delete(f"/api/lps/{lp['id']}").status_code == 204
    assert client.get(f"/api/lps/{lp['id']}").status_code == 404


def test_validation_errors(client):
    assert client.post("/api/lps", json={}).status_code == 400                      # required
    assert client.post("/api/lps", json={"name": "x", "bogus": 1}).status_code == 400  # unknown
    assert client.post("/api/lps", json={"name": "x", "lp_type": "Alien"}).status_code == 400
    assert client.post("/api/funds", json={"name": "F", "target_size": "lots"}).status_code == 400
    assert client.post("/api/funds", json={"name": "F", "first_close_date": "13/40/2025"}).status_code == 400
    assert client.get("/api/nope").status_code == 404


def test_defaults_and_stage_move(client):
    fund = post(client, "/api/funds", {"name": "Fund I"})
    assert fund["status"] == "Raising"
    lp = post(client, "/api/lps", {"name": "LP"})
    c = post(client, "/api/commitments", {"lp_id": lp["id"], "fund_id": fund["id"]})
    assert c["stage"] == "Prospect"
    c = client.patch(f"/api/commitments/{c['id']}", json={"stage": "Soft circle", "target_amount": 1e6}).json()
    assert c["stage"] == "Soft circle"
    assert client.get("/api/funds/summary").json()[0]["soft_circled"] == 1e6


def test_one_commitment_per_lp_per_fund(client):
    fund = post(client, "/api/funds", {"name": "Fund I"})
    lp = post(client, "/api/lps", {"name": "LP"})
    post(client, "/api/commitments", {"lp_id": lp["id"], "fund_id": fund["id"]})
    assert client.post("/api/commitments", json={"lp_id": lp["id"], "fund_id": fund["id"]}).status_code == 409


def test_bad_foreign_key_rejected(client):
    lp = post(client, "/api/lps", {"name": "LP"})
    assert client.post("/api/commitments", json={"lp_id": lp["id"], "fund_id": 999}).status_code == 409


def test_list_filters(client, fund_with_lps):
    fund, (a, _), _ = fund_with_lps
    rows = client.get(f"/api/commitments?lp_id={a['id']}").json()
    assert len(rows) == 1 and rows[0]["lp_id"] == a["id"]


def test_activity_log_removed_with_record(client):
    lp = post(client, "/api/lps", {"name": "LP"})
    post(client, "/api/activities", {"entity": "lps", "entity_id": lp["id"],
                                     "activity_date": "2026-01-05", "summary": "Intro call"})
    assert len(client.get(f"/api/activities?entity=lps&entity_id={lp['id']}").json()) == 1
    client.delete(f"/api/lps/{lp['id']}")
    assert client.get("/api/activities").json() == []


# ---------------------------------------------------------------- capital events

def test_capital_call_allocated_pro_rata(client, fund_with_lps):
    fund, _, (ca, cb) = fund_with_lps
    ev = post(client, "/api/capital-events", {"fund_id": fund["id"], "event_type": "Capital call",
                                              "event_date": "2025-03-01", "total_amount": 1_000_000})
    allocs = {a["commitment_id"]: a["amount"] for a in
              client.get(f"/api/capital-events/{ev['id']}/allocations").json()}
    assert allocs == {ca["id"]: 750_000, cb["id"]: 250_000}

    post(client, "/api/capital-events", {"fund_id": fund["id"], "event_type": "Distribution",
                                         "event_date": "2026-03-01", "total_amount": 400_000})
    pos = {p["lp"]: p for p in client.get("/api/lp-positions").json()}
    assert pos["LP A"]["called"] == 750_000
    assert pos["LP A"]["unfunded"] == 6_750_000
    assert pos["LP A"]["distributed"] == 300_000
    assert pos["LP A"]["dpi"] == pytest.approx(0.4)

    s = client.get("/api/funds/summary").json()[0]
    assert (s["committed"], s["called"], s["uncalled"], s["distributed"]) == (
        10_000_000, 1_000_000, 9_000_000, 400_000)
    assert s["pct_of_target"] == pytest.approx(0.2)


def test_capital_call_cannot_exceed_commitments(client, fund_with_lps):
    fund, *_ = fund_with_lps
    r = client.post("/api/capital-events", json={"fund_id": fund["id"], "event_type": "Capital call",
                                                 "event_date": "2025-03-01", "total_amount": 10_000_001})
    assert r.status_code == 400


def test_capital_call_needs_closed_lps(client):
    fund = post(client, "/api/funds", {"name": "Empty"})
    r = client.post("/api/capital-events", json={"fund_id": fund["id"], "event_type": "Capital call",
                                                 "event_date": "2025-03-01", "total_amount": 5})
    assert r.status_code == 400


def test_lp_with_capital_history_cannot_be_deleted(client, fund_with_lps):
    fund, (a, _), _ = fund_with_lps
    post(client, "/api/capital-events", {"fund_id": fund["id"], "event_type": "Capital call",
                                         "event_date": "2025-03-01", "total_amount": 100})
    assert client.delete(f"/api/lps/{a['id']}").status_code == 409
    assert client.get(f"/api/lps/{a['id']}").status_code == 200


def test_mark_allocation_paid(client, fund_with_lps):
    fund, *_ = fund_with_lps
    ev = post(client, "/api/capital-events", {"fund_id": fund["id"], "event_type": "Capital call",
                                              "event_date": "2025-03-01", "total_amount": 100})
    alloc = client.get(f"/api/capital-events/{ev['id']}/allocations").json()[0]
    assert client.patch(f"/api/allocations/{alloc['id']}", json={"paid": True}).json()["paid"] == 1
    assert client.get("/api/capital-events").json()[0]["paid_count"] == 1


# ---------------------------------------------------------------- portfolio

def test_portfolio_metrics(client, fund_with_lps):
    fund, *_ = fund_with_lps
    fid = fund["id"]
    a = post(client, "/api/companies", {"name": "Alpha", "deal_stage": "Invested", "fund_id": fid})
    b = post(client, "/api/companies", {"name": "Beta", "deal_stage": "Invested", "fund_id": fid})
    c = post(client, "/api/companies", {"name": "Gamma", "deal_stage": "Invested", "fund_id": fid})

    # Alpha: 1M, marked to 3M, then a 1M follow-on after the mark -> carried at cost.
    post(client, "/api/investments", {"company_id": a["id"], "fund_id": fid,
                                      "investment_date": "2024-01-01", "amount": 1_000_000})
    post(client, "/api/valuations", {"company_id": a["id"], "fund_id": fid,
                                     "valuation_date": "2025-01-01", "fair_value": 3_000_000})
    post(client, "/api/investments", {"company_id": a["id"], "fund_id": fid,
                                      "investment_date": "2025-06-01", "amount": 1_000_000})
    # Beta: 500K exited for 2M.
    post(client, "/api/investments", {"company_id": b["id"], "fund_id": fid,
                                      "investment_date": "2025-01-01", "amount": 500_000})
    post(client, "/api/realizations", {"company_id": b["id"], "fund_id": fid,
                                       "realization_date": "2026-01-01", "realization_type": "Full exit",
                                       "proceeds": 2_000_000})
    # Gamma: no mark -> at cost.
    post(client, "/api/investments", {"company_id": c["id"], "fund_id": fid,
                                      "investment_date": "2025-01-01", "amount": 250_000})

    data = client.get(f"/api/portfolio?fund_id={fid}").json()
    h = {r["company"]: r for r in data["holdings"]}
    assert h["Alpha"]["invested"] == 2_000_000
    assert h["Alpha"]["unrealized"] == 4_000_000
    assert h["Alpha"]["moic"] == pytest.approx(2.0)
    assert h["Beta"]["status"] == "Exited"
    assert h["Beta"]["unrealized"] == 0
    assert h["Beta"]["moic"] == pytest.approx(4.0)
    assert h["Beta"]["irr"] == pytest.approx(3.0, abs=0.01)  # 4x in one year
    assert h["Gamma"]["valuation_basis"] == "At cost"
    assert h["Gamma"]["moic"] == pytest.approx(1.0)

    t = data["totals"]
    assert t["invested"] == 2_750_000
    assert t["total_value"] == 6_250_000
    assert t["gross_moic"] == pytest.approx(6_250_000 / 2_750_000)
    assert t["active"] == 2 and t["positions"] == 3


def test_dashboard_lists_due_next_steps(client, fund_with_lps):
    fund, *_ = fund_with_lps
    post(client, "/api/companies", {"name": "Overdue Co", "next_step": "Send memo",
                                    "next_step_date": "2020-01-01"})
    post(client, "/api/companies", {"name": "Passed Co", "deal_stage": "Passed",
                                    "next_step_date": "2020-01-01"})
    post(client, "/api/companies", {"name": "Later Co", "next_step_date": "2999-01-01"})
    d = client.get("/api/dashboard").json()
    assert [t["title"] for t in d["tasks"]] == ["Overdue Co"]
    assert d["deal_pipeline"]["Sourced"] == 2
    assert d["funds"][0]["committed"] == 10_000_000


def test_ui_served(client):
    assert "VC CRM" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_connection_can_close_on_another_thread(tmp_path):
    # FastAPI may run a dependency's setup and teardown on different threads.
    import threading

    from vc_crm.db import connect, init_db

    path = str(tmp_path / "t.db")
    init_db(path)
    conn = connect(path)
    err = []

    def use():
        try:
            conn.execute("SELECT 1")
            conn.close()
        except Exception as e:
            err.append(e)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert not err
