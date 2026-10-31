"""Fund economics checked against hand-worked numbers."""

import pytest

from vc_crm import economics

from .conftest import signed_in

D = economics._d


# ---------------------------------------------------------------- waterfall (pure function)

def test_waterfall_full_catch_up():
    # LPs put in 100 a year ago; NAV 150; 8% hurdle, 20% carry, 100% catch-up.
    w = economics.european_carry([("2025-01-01", 100)], [], 150, "2026-01-01", 20, 8, 100)
    # capital 100 -> pref 8 -> catch-up 2 (all to GP) -> remaining 40 split 32/8
    assert w["return_of_capital"] == pytest.approx(100)
    assert w["preferred_return"] == pytest.approx(8)
    assert w["catch_up"] == pytest.approx(2)
    assert w["gp_carry"] == pytest.approx(10)  # = 20% of the 50 profit
    assert w["lp_value"] == pytest.approx(140)


def test_waterfall_below_hurdle_and_no_hurdle():
    below = economics.european_carry([("2025-01-01", 100)], [], 105, "2026-01-01", 20, 8, 100)
    assert below["gp_carry"] == 0 and below["lp_value"] == pytest.approx(105)
    loss = economics.european_carry([("2025-01-01", 100)], [], 80, "2026-01-01", 20, 8, 100)
    assert loss["gp_carry"] == 0
    flat = economics.european_carry([("2025-01-01", 100)], [], 150, "2026-01-01", 20, None, None)
    assert flat["gp_carry"] == pytest.approx(10)  # 20% of 50, no hurdle
    none = economics.european_carry([("2025-01-01", 100)], [], 150, "2026-01-01", None, 8, 100)
    assert none["gp_carry"] == 0


def test_waterfall_partial_catch_up_and_prior_distributions():
    # 80% catch-up: GP gets 80c of each dollar until it has 20% of profit.
    w = economics.european_carry([("2025-01-01", 100)], [], 150, "2026-01-01", 20, 8, 80)
    # pref 8 -> z = 0.2*8/(0.8-0.2) = 2.667, GP 2.133 -> remaining 39.333 split -> GP 7.867
    assert w["gp_carry"] == pytest.approx(10)
    # 60 already distributed (to LPs): only 40 capital left to return from NAV.
    d = economics.european_carry([("2025-01-01", 100)], [("2025-07-02", 60)], 90, "2026-01-01", 20, None, None)
    assert d["return_of_capital"] == pytest.approx(40)
    assert d["gp_carry"] == pytest.approx(10)  # 20% of total profit 50


# ---------------------------------------------------------------- fees (pure function)

def test_fees_switch_basis_after_investment_period():
    fund = {"mgmt_fee_rate": 2, "fee_start_date": "2025-01-01", "investment_period_end": "2025-07-02",
            "post_ip_fee_rate": 1.5, "post_ip_fee_basis": "Invested cost (active)"}
    invs = [(1, D("2025-01-01"), 4_000_000)]
    total, by_year = economics.fees_accrued(fund, 10_000_000, 10_000_000, invs, {}, "2026-01-01")
    expected = 0.02 * 10e6 * 182 / 365 + 0.015 * 4e6 * 183 / 365
    assert total == pytest.approx(expected)
    assert by_year == {2025: pytest.approx(expected)}
    # Exiting the position stops the post-period fee on it.
    total2, _ = economics.fees_accrued(fund, 10e6, 10e6, invs, {1: D("2025-10-01")}, "2026-01-01")
    assert total2 == pytest.approx(0.02 * 10e6 * 182 / 365 + 0.015 * 4e6 * 91 / 365)
    assert economics.fees_accrued({"mgmt_fee_rate": None}, 1, 1, [], {}, "2026-01-01")[0] == 0


# ---------------------------------------------------------------- whole fund through the API

def setup_fund(client):
    f = client.post("/api/funds", json={
        "name": "Fund I", "first_close_date": "2025-01-01", "mgmt_fee_rate": 2, "carry_rate": 20,
        "hurdle_rate": 8, "catch_up_rate": 100}).json()
    lps = {}
    for name, amt, exempt in [("LP A", 6_000_000, "No"), ("LP B", 3_000_000, "No"), ("GP Commit", 1_000_000, "Yes")]:
        lp = client.post("/api/lps", json={"name": name}).json()
        lps[name] = client.post("/api/commitments", json={
            "lp_id": lp["id"], "fund_id": f["id"], "stage": "Closed", "committed_amount": amt,
            "fee_carry_exempt": exempt}).json()
    client.post("/api/capital-events", json={"fund_id": f["id"], "event_type": "Capital call",
                                             "event_date": "2025-01-01", "total_amount": 5_000_000})
    co = client.post("/api/companies", json={"name": "Acme", "fund_id": f["id"]}).json()
    client.post("/api/investments", json={"company_id": co["id"], "fund_id": f["id"],
                                          "investment_date": "2025-01-02", "amount": 4_000_000})
    client.post("/api/valuations", json={"company_id": co["id"], "fund_id": f["id"],
                                         "valuation_date": "2025-12-31", "fair_value": 10_000_000})
    return f, lps


def test_fund_economics(client):
    f, lps = setup_fund(client)
    e = client.get(f"/api/funds/{f['id']}/economics?as_of=2026-01-01").json()
    assert e["missing_terms"] == [] and e["warnings"] == []
    fees = 0.02 * 9_000_000  # one year on fee-paying commitments (GP commit exempt)
    assert e["fees_accrued"] == pytest.approx(fees)
    assert e["cash_estimate"] == pytest.approx(5e6 - 4e6 - fees)
    nav = 10e6 + 5e6 - 4e6 - fees
    assert e["nav_before_carry"] == pytest.approx(nav)
    by = {x["lp"]: x for x in e["lps"]}
    # Paid in pro rata to commitment: 3.0M / 1.5M / 0.5M.
    assert [by[n]["paid_in"] for n in ("LP A", "LP B", "GP Commit")] == [3e6, 1.5e6, 0.5e6]
    assert by["GP Commit"]["fees"] == 0 and by["GP Commit"]["carry"] == 0
    assert by["LP A"]["fees"] == pytest.approx(fees * 6 / 9)
    gp_nav = (nav + fees) * 0.1
    assert by["GP Commit"]["nav"] == pytest.approx(gp_nav)
    # Carry-paying LPs: contributed 4.5M, value before carry = 90% of pre-fee NAV - all fees.
    payer_value = (nav + fees) * 0.9 - fees
    w = economics.european_carry([("2025-01-01", 4.5e6)], [], payer_value, "2026-01-01", 20, 8, 100)
    assert e["carry_accrued"] == pytest.approx(w["gp_carry"])
    assert w["gp_carry"] == pytest.approx(0.2 * (payer_value - 4.5e6))  # hurdle cleared, fully caught up
    assert by["LP A"]["carry"] + by["LP B"]["carry"] == pytest.approx(w["gp_carry"])
    assert by["LP A"]["nav"] == pytest.approx(by["LP A"]["nav_before_carry"] - by["LP A"]["carry"])
    assert by["LP A"]["tvpi"] == pytest.approx(by["LP A"]["nav"] / 3e6)
    assert by["LP A"]["net_irr"] == pytest.approx(by["LP A"]["tvpi"] - 1, abs=0.01)  # one year
    # Net is below gross.
    assert e["net_tvpi"] < e["gross_moic"]
    # A date before the mark ignores it.
    early = client.get(f"/api/funds/{f['id']}/economics?as_of=2025-06-30").json()
    assert early["unrealized"] == pytest.approx(4e6)
    assert client.get(f"/api/funds/{f['id']}/economics?as_of=junk").status_code == 400


def test_missing_terms_and_negative_cash(client):
    f = client.post("/api/funds", json={"name": "Bare"}).json()
    lp = client.post("/api/lps", json={"name": "LP"}).json()
    client.post("/api/commitments", json={"lp_id": lp["id"], "fund_id": f["id"], "stage": "Closed",
                                          "committed_amount": 1e6})
    co = client.post("/api/companies", json={"name": "X", "fund_id": f["id"]}).json()
    client.post("/api/investments", json={"company_id": co["id"], "fund_id": f["id"],
                                          "investment_date": "2025-01-01", "amount": 5e5})
    e = client.get(f"/api/funds/{f['id']}/economics").json()
    assert set(e["missing_terms"]) == {"management fee", "carried interest"}
    assert e["fees_accrued"] == 0 and e["carry_accrued"] == 0
    assert e["warnings"] and "capital calls" in e["warnings"][0]


def test_reports(client, app, database_url):
    f, lps = setup_fund(client)
    client.patch(f"/api/funds/{f['id']}", json={"wire_instructions": "Bank: Test Bank\nAcct: 123"})
    r = client.get(f"/reports/statement/{lps['LP A']['id']}?as_of=2026-01-01")
    assert r.status_code == 200 and "Capital account statement" in r.text and "LP A" in r.text
    assert "$3,000,000" in r.text  # paid-in
    assert "<script src=\"/static/print.js\">" in r.text
    all_ = client.get(f"/reports/fund/{f['id']}/statements?as_of=2026-01-01").text
    assert all_.count("Capital account statement") == 3
    ev = client.get("/api/capital-events").json()[0]
    alloc = [a for a in client.get(f"/api/capital-events/{ev['id']}/allocations").json() if a["lp"] == "LP B"][0]
    n = client.get(f"/reports/notice/{alloc['id']}").text
    assert "Capital call notice" in n and "$1,500,000.00" in n and "Acct: 123" in n
    assert "Remaining unfunded commitment" in n and "$1,500,000" in n
    # Names are escaped.
    client.patch(f"/api/lps/{lps['LP A']['lp_id']}", json={"name": "<b>LP A</b>"})
    assert "<b>LP A</b>" not in client.get(f"/reports/statement/{lps['LP A']['id']}").text
    # Fund access applies.
    other = client.post("/api/funds", json={"name": "Other"}).json()
    u = signed_in(app, database_url, "u@firm.com", "viewer")
    uid = u.get("/api/auth/me").json()["id"]
    client.put(f"/api/users/{uid}/funds", json={str(other["id"]): "viewer"})
    assert u.get(f"/reports/statement/{lps['LP A']['id']}").status_code == 404
    assert u.get(f"/api/funds/{f['id']}/economics").status_code == 404
