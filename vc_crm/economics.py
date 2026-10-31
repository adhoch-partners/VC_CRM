"""Fund economics: management fees, a European (whole-fund) waterfall, and net LP returns.

Everything is computed from the fund's own terms and records; no term is assumed. If a
term is blank, that part (fees, hurdle, catch-up, carry) is simply zero and the result says
which terms are missing.

Method (all as of a date):
  * Fees accrue daily: rate x basis / 365. During the investment period the basis is the
    fee-paying committed capital; afterwards it is either committed capital or the cost of
    investments still held, at the post-period rate (or the same rate if none is set).
    Later-closing LPs are treated as paying from the fee start date (equalized).
    Commitments marked "No fees or carry" pay neither.
  * Fund cash is estimated as: capital called - invested cost - fees + exit proceeds
    - distributions. Fees are assumed to be paid out of called capital.
  * Fund NAV = unrealized portfolio value + estimated cash.
  * Each LP's share of NAV before fees and carry is pro rata to capital paid in; fees are
    then charged to fee-paying LPs pro rata to their commitments.
  * Carry is what the GP would receive if the fund sold everything at NAV today, through a
    whole-fund waterfall on the carry-paying LPs: 1) return of contributed capital,
    2) preferred return compounding annually at the hurdle on each contribution (net of
    distributions), 3) GP catch-up at the catch-up rate until the GP has the carry share of
    all profit, 4) split at the carry rate. Distributions to date are assumed to have gone
    entirely to LPs (no carry paid yet).
"""

from collections import defaultdict
from datetime import date, timedelta

from . import metrics

TERMINAL = metrics.TERMINAL_REALIZATIONS


def _d(s):
    return date.fromisoformat(str(s)[:10])


def _years(a, b):
    return (b - a).days / 365.0


def pct(v):
    return (v or 0) / 100.0


# ---------------------------------------------------------------- fees

def active_cost_at(investments, terminal, day):
    """Cost of investments made on or before `day` in positions not fully exited by then."""
    return sum(amt for cid, d_, amt in investments
               if d_ <= day and not (cid in terminal and terminal[cid] <= day))


def fees_accrued(fund, fee_commitment, total_commitment, investments, terminal, as_of):
    """(total fees accrued to as_of, {year: fees}) for the whole fund."""
    rate = pct(fund.get("mgmt_fee_rate"))
    start = fund.get("fee_start_date") or fund.get("first_close_date")
    if not rate or not start or not fee_commitment:
        return 0.0, {}
    start, end = _d(start), _d(as_of)
    ip_end = _d(fund["investment_period_end"]) if fund.get("investment_period_end") else None
    post_rate = pct(fund.get("post_ip_fee_rate")) if fund.get("post_ip_fee_rate") is not None else rate
    by_invested = fund.get("post_ip_fee_basis") == "Invested cost (active)"
    paying_share = fee_commitment / total_commitment if total_commitment else 0.0
    total, by_year = 0.0, defaultdict(float)
    day = start
    while day < end:
        if ip_end is None or day < ip_end:
            fee = rate * fee_commitment / 365
        elif by_invested:
            fee = post_rate * active_cost_at(investments, terminal, day) * paying_share / 365
        else:
            fee = post_rate * fee_commitment / 365
        total += fee
        by_year[day.year] += fee
        day += timedelta(days=1)
    return total, dict(by_year)


# ---------------------------------------------------------------- waterfall

def european_carry(contributions, distributions, nav, as_of, carry_rate, hurdle_rate, catch_up_rate):
    """GP carry if the fund were liquidated at `nav` on `as_of`.

    contributions / distributions: [(iso date, amount > 0)] for carry-paying LPs.
    Returns a dict with the split of `nav` between LPs and GP, tier by tier.
    """
    carry, hurdle, k = pct(carry_rate), pct(hurdle_rate), pct(catch_up_rate)
    end = _d(as_of)
    paid_in = sum(a for _, a in contributions)
    paid_out = sum(a for _, a in distributions)
    out = {"return_of_capital": 0.0, "preferred_return": 0.0, "catch_up": 0.0,
           "split_lp": 0.0, "split_gp": 0.0}
    remaining = max(nav, 0.0)
    if not carry or remaining <= 0:
        out["split_lp"] = remaining
        out["gp_carry"] = 0.0
        out["lp_value"] = remaining
        return out
    # 1) unreturned capital
    unreturned = max(0.0, paid_in - paid_out)
    a = min(remaining, unreturned)
    remaining -= a
    # 2) preferred return: what LPs need at as_of for an IRR equal to the hurdle
    if hurdle:
        fv_in = sum(x * (1 + hurdle) ** _years(_d(t), end) for t, x in contributions)
        fv_out = sum(x * (1 + hurdle) ** _years(_d(t), end) for t, x in distributions)
        pref_needed = max(0.0, fv_in - fv_out - unreturned)
    else:
        pref_needed = 0.0
    b = min(remaining, pref_needed)
    remaining -= b
    # 3) catch-up: GP takes k of each dollar until it has `carry` of all profit
    lp_profit = paid_out + a + b - paid_in
    gp = 0.0
    z = 0.0
    if hurdle and k > carry and lp_profit > 0:
        z = min(remaining, carry * lp_profit / (k - carry))
        gp += k * z
        remaining -= z
    # 4) split
    split_gp = carry * remaining
    gp += split_gp
    out.update(return_of_capital=a, preferred_return=b, catch_up=z,
               split_lp=remaining - split_gp, split_gp=split_gp)
    out["gp_carry"] = gp
    out["lp_value"] = max(nav, 0.0) - gp
    return out


# ---------------------------------------------------------------- fund + LP accounts

def compute(conn, fund, as_of=None):
    as_of = as_of or date.today().isoformat()
    fid = fund["id"]
    missing = [label for key, label in [("mgmt_fee_rate", "management fee"),
                                         ("carry_rate", "carried interest")]
               if fund.get(key) is None]
    if fund.get("mgmt_fee_rate") and not (fund.get("fee_start_date") or fund.get("first_close_date")):
        missing.append("fee start date (or first close)")

    commits = [dict(r) for r in conn.execute(
        "SELECT c.id, c.lp_id, l.name AS lp, c.committed_amount, c.fee_carry_exempt "
        "FROM commitments c JOIN lps l ON l.id = c.lp_id "
        "WHERE c.fund_id = ? AND c.stage = 'Closed' ORDER BY l.name", (fid,))]
    flows = defaultdict(lambda: {"calls": [], "dists": []})
    for r in conn.execute(
        "SELECT a.commitment_id, a.amount, e.event_type, e.event_date FROM capital_event_allocations a "
        "JOIN capital_events e ON e.id = a.event_id WHERE e.fund_id = ? AND e.event_date <= ? "
        "ORDER BY e.event_date, e.id", (fid, as_of)):
        key = "calls" if r["event_type"] == "Capital call" else "dists"
        flows[r["commitment_id"]][key].append((r["event_date"], r["amount"]))

    investments = [(r["company_id"], _d(r["investment_date"]), r["amount"]) for r in conn.execute(
        "SELECT company_id, investment_date, amount FROM investments WHERE fund_id = ? "
        "AND investment_date <= ?", (fid, as_of))]
    terminal = {}
    for r in conn.execute("SELECT company_id, realization_date, realization_type FROM realizations "
                          "WHERE fund_id = ? AND realization_date <= ?", (fid, as_of)):
        if r["realization_type"] in TERMINAL:
            d_ = _d(r["realization_date"])
            terminal[r["company_id"]] = min(terminal.get(r["company_id"], d_), d_)
    holdings = metrics.holdings(conn, fid, as_of)
    unrealized = sum(h["unrealized"] for h in holdings)
    proceeds = sum(h["realized"] for h in holdings)
    invested = sum(h["invested"] for h in holdings)

    total_commit = sum(c["committed_amount"] or 0 for c in commits)
    fee_commit = sum(c["committed_amount"] or 0 for c in commits if c["fee_carry_exempt"] != "Yes")
    fees, fees_by_year = fees_accrued(fund, fee_commit, total_commit, investments, terminal, as_of)

    called = sum(a for f in flows.values() for _, a in f["calls"])
    distributed = sum(a for f in flows.values() for _, a in f["dists"])
    cash = called - invested - fees + proceeds - distributed
    nav_gross = unrealized + cash  # after fees, before carry
    warnings = []
    if cash < -0.5:
        warnings.append("Recorded capital calls do not cover the investments and fees to date, so "
                        "estimated cash is negative. Record the missing capital calls to make "
                        "NAV and net returns accurate.")

    # LP shares of NAV before carry
    nav_before_fees = nav_gross + fees
    lps = []
    for c in commits:
        f = flows[c["id"]]
        paid_in = sum(a for _, a in f["calls"])
        dist = sum(a for _, a in f["dists"])
        exempt = c["fee_carry_exempt"] == "Yes"
        fee_share = 0.0 if exempt or not fee_commit else fees * (c["committed_amount"] or 0) / fee_commit
        nav_share = (nav_before_fees * paid_in / called if called else 0.0) - fee_share
        lps.append({"commitment_id": c["id"], "lp_id": c["lp_id"], "lp": c["lp"],
                    "committed": c["committed_amount"] or 0, "exempt": exempt,
                    "paid_in": paid_in, "unfunded": (c["committed_amount"] or 0) - paid_in,
                    "distributed": dist, "fees": fee_share, "nav_before_carry": nav_share,
                    "calls": f["calls"], "dists": f["dists"]})

    payers = [x for x in lps if not x["exempt"]]
    wf = european_carry(
        [cf for x in payers for cf in x["calls"]], [cf for x in payers for cf in x["dists"]],
        sum(x["nav_before_carry"] for x in payers), as_of,
        fund.get("carry_rate"), fund.get("hurdle_rate"), fund.get("catch_up_rate"))
    payer_nav = sum(max(x["nav_before_carry"], 0) for x in payers)
    for x in lps:
        share = 0.0 if x["exempt"] or payer_nav <= 0 else max(x["nav_before_carry"], 0) / payer_nav
        x["carry"] = wf["gp_carry"] * share
        x["nav"] = x["nav_before_carry"] - x["carry"]
        p = x["paid_in"]
        x["dpi"] = x["distributed"] / p if p else None
        x["rvpi"] = x["nav"] / p if p else None
        x["tvpi"] = (x["distributed"] + x["nav"]) / p if p else None
        cfs = [(t, -a) for t, a in x["calls"]] + [(t, a) for t, a in x["dists"]]
        if x["nav"] > 0:
            cfs.append((as_of, x["nav"]))
        x["net_irr"] = metrics.xirr(cfs)

    all_cfs = [(t, -a) for x in lps for t, a in x["calls"]] + [(t, a) for x in lps for t, a in x["dists"]]
    nav_net = sum(x["nav"] for x in lps)
    if nav_net > 0:
        all_cfs.append((as_of, nav_net))
    gross = metrics.portfolio_totals(holdings)
    for x in lps:
        x.pop("calls")
        x.pop("dists")
    return {
        "fund_id": fid, "as_of": as_of, "missing_terms": missing, "warnings": warnings,
        "committed": total_commit, "fee_paying_committed": fee_commit,
        "called": called, "distributed": distributed, "invested": invested,
        "proceeds": proceeds, "unrealized": unrealized,
        "fees_accrued": fees, "fees_by_year": {str(k): v for k, v in sorted(fees_by_year.items())},
        "annual_fee_now": _annual_fee_now(fund, fee_commit, total_commit, investments, terminal, as_of),
        "cash_estimate": cash, "nav_before_carry": nav_gross,
        "waterfall": wf, "carry_accrued": wf["gp_carry"], "nav_net": nav_net,
        "net_dpi": distributed / called if called else None,
        "net_tvpi": (distributed + nav_net) / called if called else None,
        "net_irr": metrics.xirr(all_cfs),
        "gross_moic": gross["gross_moic"], "gross_irr": gross["gross_irr"],
        "lps": lps,
    }


def _annual_fee_now(fund, fee_commit, total_commit, investments, terminal, as_of):
    rate = pct(fund.get("mgmt_fee_rate"))
    if not rate:
        return 0.0
    day = _d(as_of)
    ip_end = _d(fund["investment_period_end"]) if fund.get("investment_period_end") else None
    if ip_end is None or day < ip_end:
        return rate * fee_commit
    post = pct(fund.get("post_ip_fee_rate")) if fund.get("post_ip_fee_rate") is not None else rate
    if fund.get("post_ip_fee_basis") == "Invested cost (active)":
        share = fee_commit / total_commit if total_commit else 0
        return post * active_cost_at(investments, terminal, day) * share
    return post * fee_commit
