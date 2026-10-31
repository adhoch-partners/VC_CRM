"""Portfolio and fund performance calculations.

All figures here are GROSS at the portfolio level (before fees and carry) except
LP paid-in / distributed / DPI, which come straight from capital events.
"""

from collections import defaultdict
from datetime import date

TERMINAL_REALIZATIONS = {"Full exit", "Write-off"}


def _d(s):
    return date.fromisoformat(s[:10])


def xirr(cashflows):
    """Annualised IRR for [(iso_date, amount), ...]. Negative = money out.

    Returns None when the IRR is undefined (no sign change, or < 2 flows).
    Uses bisection, which is slower than Newton but cannot diverge.
    """
    flows = [(_d(dt), amt) for dt, amt in cashflows if amt]
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = min(dt for dt, _ in flows)
    years = [((dt - t0).days / 365.0, amt) for dt, amt in flows]

    def npv(rate):
        return sum(amt / (1.0 + rate) ** t for t, amt in years)

    lo, hi = -0.9999, 10.0
    try:
        f_lo, f_hi = npv(lo), npv(hi)
        # Widen the upper bound for very high returns on short holding periods.
        while f_lo * f_hi > 0 and hi < 1e6:
            hi *= 10
            f_hi = npv(hi)
    except (OverflowError, ZeroDivisionError):
        return None
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return mid


def _ratio(num, den):
    return num / den if den else None


def holdings(conn, fund_id=None, as_of=None):
    """One row per (company, fund) position with cost, value and returns.

    With an explicit `as_of`, investments, exits and marks dated after it are ignored,
    so a past date shows the portfolio as it stood then."""
    cutoff = as_of
    as_of = as_of or date.today().isoformat()
    where, args = ("WHERE fund_id = ?", [fund_id]) if fund_id else ("", [])

    def dated(col):
        """WHERE clause and args, adding the as-of cutoff on `col` when one was given."""
        if not cutoff:
            return where, args
        return ((where + " AND " if where else "WHERE ") + f"{col} <= ?", [*args, cutoff])

    flows = defaultdict(list)
    invested = defaultdict(float)
    last_invest = {}
    for r in conn.execute(
        "SELECT company_id, fund_id, investment_date, amount FROM investments "
        + dated("investment_date")[0], dated("investment_date")[1]
    ):
        k = (r["company_id"], r["fund_id"])
        invested[k] += r["amount"]
        flows[k].append((r["investment_date"], -r["amount"]))
        last_invest[k] = max(last_invest.get(k, ""), r["investment_date"])

    realized = defaultdict(float)
    terminal = {}
    for r in conn.execute(
        "SELECT company_id, fund_id, realization_date, realization_type, proceeds "
        "FROM realizations " + dated("realization_date")[0], dated("realization_date")[1]
    ):
        k = (r["company_id"], r["fund_id"])
        realized[k] += r["proceeds"]
        if r["proceeds"]:
            flows[k].append((r["realization_date"], r["proceeds"]))
        if r["realization_type"] in TERMINAL_REALIZATIONS:
            terminal[k] = max(terminal.get(k, ""), r["realization_date"])

    latest_val = {}
    for r in conn.execute(
        "SELECT company_id, fund_id, valuation_date, fair_value FROM valuations "
        + dated("valuation_date")[0] + " ORDER BY valuation_date, id", dated("valuation_date")[1]
    ):
        latest_val[(r["company_id"], r["fund_id"])] = (r["valuation_date"], r["fair_value"])

    keys = set(invested) | set(realized)
    names = {r["id"]: r for r in conn.execute("SELECT id, name, sector FROM companies")}
    fund_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM funds")}

    rows = []
    for k in keys:
        company_id, fid = k
        cost = invested[k]
        val = latest_val.get(k)
        if k in terminal and (not val or val[0] <= terminal[k]):
            unrealized, basis = 0.0, "Realized"
        elif val:
            # Money invested after the last mark is carried at cost on top of it.
            later = sum(
                -a for d_, a in flows[k] if a < 0 and d_ > val[0]
            )
            unrealized, basis = val[1] + later, f"Marked {val[0]}"
        else:
            unrealized, basis = cost, "At cost"
        total = realized[k] + unrealized
        cf = list(flows[k])
        if unrealized:
            cf.append((as_of, unrealized))
        rows.append({
            "company_id": company_id,
            "company": names[company_id]["name"] if company_id in names else None,
            "sector": names[company_id]["sector"] if company_id in names else None,
            "fund_id": fid,
            "fund": fund_names.get(fid),
            "invested": cost,
            "realized": realized[k],
            "unrealized": unrealized,
            "total_value": total,
            "moic": _ratio(total, cost),
            "irr": xirr(cf),
            "valuation_basis": basis,
            "first_investment": min((d_ for d_, a in flows[k] if a < 0), default=None),
            "last_investment": last_invest.get(k),
            "status": "Exited" if basis == "Realized" else "Active",
            "_flows": cf,
        })
    rows.sort(key=lambda r: (r["fund"] or "", r["company"] or ""))
    return rows


def portfolio_totals(rows):
    invested = sum(r["invested"] for r in rows)
    realized = sum(r["realized"] for r in rows)
    unrealized = sum(r["unrealized"] for r in rows)
    flows = [cf for r in rows for cf in r["_flows"]]
    return {
        "positions": len(rows),
        "active": sum(1 for r in rows if r["status"] == "Active"),
        "invested": invested,
        "realized": realized,
        "unrealized": unrealized,
        "total_value": realized + unrealized,
        "gross_moic": _ratio(realized + unrealized, invested),
        "gross_irr": xirr(flows),
    }


def fund_summary(conn, fund):
    fid = fund["id"]
    by_stage = {
        r["stage"]: {"count": r["n"], "target": r["target"] or 0, "committed": r["committed"] or 0}
        for r in conn.execute(
            "SELECT stage, COUNT(*) n, SUM(target_amount) target, SUM(committed_amount) committed "
            "FROM commitments WHERE fund_id = ? GROUP BY stage", (fid,)
        )
    }
    committed = by_stage.get("Closed", {}).get("committed", 0)
    soft = sum(by_stage.get(s, {}).get("target", 0) for s in ("Soft circle", "Legal / docs"))
    ev = {
        r["event_type"]: r["total"]
        for r in conn.execute(
            "SELECT event_type, SUM(total_amount) total FROM capital_events "
            "WHERE fund_id = ? GROUP BY event_type", (fid,)
        )
    }
    called = ev.get("Capital call", 0) or 0
    distributed = ev.get("Distribution", 0) or 0
    port = portfolio_totals(holdings(conn, fid))
    return {
        **dict(fund),
        "lp_pipeline": by_stage,
        "committed": committed,
        "soft_circled": soft,
        "pct_of_target": _ratio(committed, fund["target_size"]),
        "closed_lps": by_stage.get("Closed", {}).get("count", 0),
        "called": called,
        "uncalled": committed - called,
        "distributed": distributed,
        "dpi": _ratio(distributed, called),
        "portfolio": port,
        "dry_powder": called - port["invested"],
    }


def lp_positions(conn, fund_id=None):
    """Closed commitments with paid-in, distributed and unfunded per LP."""
    where, args = ("AND c.fund_id = ?", [fund_id]) if fund_id else ("", [])
    rows = conn.execute(
        f"""
        SELECT c.id commitment_id, c.lp_id, l.name lp, c.fund_id, f.name fund,
               c.committed_amount committed,
               COALESCE(SUM(CASE WHEN e.event_type = 'Capital call' THEN a.amount END), 0) called,
               COALESCE(SUM(CASE WHEN e.event_type = 'Distribution' THEN a.amount END), 0) distributed
        FROM commitments c
        JOIN lps l ON l.id = c.lp_id
        JOIN funds f ON f.id = c.fund_id
        LEFT JOIN capital_event_allocations a ON a.commitment_id = c.id
        LEFT JOIN capital_events e ON e.id = a.event_id
        WHERE c.stage = 'Closed' {where}
        GROUP BY c.id, l.name, f.name
        ORDER BY f.name, l.name
        """,
        args,
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["committed"] = d["committed"] or 0
        d["unfunded"] = d["committed"] - d["called"]
        d["pct_called"] = _ratio(d["called"], d["committed"])
        d["dpi"] = _ratio(d["distributed"], d["called"])
        out.append(d)
    return out


def allocate(total, weights):
    """Split `total` pro rata over {key: weight}, rounded to cents, summing exactly."""
    wsum = sum(weights.values())
    if wsum <= 0:
        raise ValueError("no positive weights to allocate against")
    cents = round(total * 100)
    raw = {k: cents * w / wsum for k, w in weights.items()}
    out = {k: int(v) for k, v in raw.items()}
    # Largest-remainder method so the pennies land deterministically.
    leftover = cents - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), k))[:leftover]:
        out[k] += 1
    return {k: v / 100 for k, v in out.items()}
