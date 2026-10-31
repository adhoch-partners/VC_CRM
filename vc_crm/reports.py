"""Fund economics API and printable LP documents (statements and notices).

Documents are plain HTML pages styled for printing; use the browser's Print > Save as PDF.
"""

from datetime import date
from html import escape

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse

from . import economics


def money(v, cents=False):
    if v is None:
        return ""
    s = f"{abs(v):,.2f}" if cents else f"{abs(v):,.0f}"
    return f"-${s}" if v < 0 else f"${s}"


def ratio(v):
    return "n/a" if v is None else f"{v:.2f}x"


def pct(v):
    return "n/a" if v is None else f"{v * 100:.1f}%"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
  body {{ font: 13px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; color: #111; margin: 0; background: #f3f4f6; }}
  .page {{ background: #fff; max-width: 800px; margin: 24px auto; padding: 40px 48px; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  h1 {{ font-size: 20px; margin: 0 0 2px; }} h2 {{ font-size: 14px; margin: 22px 0 6px; border-bottom: 1px solid #ccc; padding-bottom: 3px; }}
  .muted {{ color: #555; }} table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
  td, th {{ padding: 4px 6px; border-bottom: 1px solid #eee; text-align: left; }} th {{ font-weight: 600; }}
  td.n, th.n {{ text-align: right; }} tr.total td {{ font-weight: 700; border-top: 1px solid #999; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0 32px; }}
  .note {{ font-size: 11px; color: #555; margin-top: 18px; }} pre {{ font: inherit; white-space: pre-wrap; margin: 0; }}
  .bar {{ max-width: 800px; margin: 16px auto 0; display: flex; gap: 8px; }}
  .bar button {{ font: inherit; padding: 6px 12px; border-radius: 6px; border: 1px solid #ccc; background: #fff; cursor: pointer; }}
  @media print {{ body {{ background: #fff; }} .bar {{ display: none; }}
    .page {{ box-shadow: none; margin: 0; max-width: none; padding: 0; page-break-after: always; }} }}
</style></head><body>
<div class="bar"><button data-print>Print / Save as PDF</button></div>
{body}
<script src="/static/print.js"></script></body></html>"""


def kv(rows):
    return "<table>" + "".join(f"<tr><td>{escape(k)}</td><td class='n'>{v}</td></tr>" for k, v in rows) + "</table>"


def statement_html(fund, eco, lp, activity):
    terms = []
    for key, label in [("mgmt_fee_rate", "Management fee"), ("carry_rate", "Carried interest"),
                       ("hurdle_rate", "Preferred return"), ("catch_up_rate", "GP catch-up")]:
        if fund.get(key) is not None:
            terms.append(f"{label} {fund[key]:g}%")
    rows = "".join(
        f"<tr><td>{escape(d)}</td><td>{escape(t)}</td><td class='n'>{money(a, True)}</td></tr>"
        for d, t, a in activity) or "<tr><td colspan='3' class='muted'>No capital activity.</td></tr>"
    exempt = " (no fees or carry)" if lp["exempt"] else ""
    return f"""<div class="page">
  <h1>Capital account statement</h1>
  <div class="muted">{escape(fund['name'])} · {escape(lp['lp'])}{exempt} · as of {escape(eco['as_of'])}</div>
  <div class="grid2">
    <div><h2>Commitment</h2>{kv([("Commitment", money(lp['committed'])), ("Paid-in capital", money(lp['paid_in'])),
        ("Unfunded commitment", money(lp['unfunded'])), ("% called", pct(lp['paid_in'] / lp['committed'] if lp['committed'] else None))])}</div>
    <div><h2>Capital account</h2>{kv([("Paid-in capital", money(lp['paid_in'])), ("Distributions", money(-lp['distributed'])),
        ("Management fees (share, accrued)", money(-lp['fees'])), ("Investment gains / (losses)", money(lp['nav_before_carry'] + lp['distributed'] - lp['paid_in'] + lp['fees'])),
        ("Accrued carried interest", money(-lp['carry'])), ("Ending capital (NAV)", f"<b>{money(lp['nav'])}</b>")])}</div>
  </div>
  <h2>Performance (net of fees and carry)</h2>
  {kv([("DPI (distributions / paid-in)", ratio(lp['dpi'])), ("RVPI (NAV / paid-in)", ratio(lp['rvpi'])),
       ("TVPI (total value / paid-in)", ratio(lp['tvpi'])), ("Net IRR", pct(lp['net_irr']))])}
  <h2>Capital activity</h2>
  <table><tr><th>Date</th><th>Type</th><th class="n">Amount</th></tr>{rows}</table>
  <h2>Fund summary</h2>
  {kv([("Fund commitments", money(eco['committed'])), ("Called to date", money(eco['called'])),
       ("Distributed to date", money(eco['distributed'])), ("Fund NAV (net of accrued carry)", money(eco['nav_net'])),
       ("Fund net TVPI", ratio(eco['net_tvpi'])), ("Fund net IRR", pct(eco['net_irr']))])}
  <div class="note">{escape(" · ".join(terms))}{"<br>" if terms else ""}Unaudited. NAV reflects the general partner's
  most recent valuations; accrued carried interest assumes a sale of all investments at NAV on the statement date.
  Management fees are accrued from the fund's recorded terms. Prepared {date.today().isoformat()}.</div>
</div>"""


def notice_html(fund, event, alloc, lp, totals):
    is_call = event["event_type"] == "Capital call"
    title = "Capital call notice" if is_call else "Distribution notice"
    lines = [("Your share of this " + ("call" if is_call else "distribution"), money(alloc["amount"], True))]
    if is_call:
        lines += [("Due date", escape(event["due_date"] or "On receipt")),
                  ("Commitment", money(lp["committed_amount"])),
                  ("Called to date, including this notice", money(totals["called"])),
                  ("Remaining unfunded commitment", money((lp["committed_amount"] or 0) - totals["called"]))]
    else:
        lines += [("Distributions to date, including this one", money(totals["distributed"]))]
    wire = (f"<h2>Wire instructions</h2><pre>{escape(fund['wire_instructions'])}</pre>"
            if is_call and fund.get("wire_instructions") else "")
    purpose = f"<p>Purpose: {escape(event['purpose'])}</p>" if event.get("purpose") else ""
    return f"""<div class="page">
  <h1>{title}</h1>
  <div class="muted">{escape(fund['name'])} · {escape(lp['lp'])} · notice date {escape(event['event_date'])}</div>
  <p>The fund is {"calling" if is_call else "distributing"} {money(event['total_amount'], True)} in total, allocated
  pro rata to commitments.</p>{purpose}
  {kv(lines)}
  {wire}
  <div class="note">Questions about this notice? Reply to your fund contact. Prepared {date.today().isoformat()}.</div>
</div>"""


def register(app, d):
    get_conn, get_access = d.get_conn, d.get_access

    def visible_fund(conn, acc, fid):
        fund = d.fetch(conn, "funds", fid)
        if not acc.can_view_fund(fid):
            raise HTTPException(404, "Fund not found")
        return fund

    def check_date(as_of):
        if as_of is None:
            return None
        try:
            return date.fromisoformat(as_of).isoformat()
        except ValueError:
            raise HTTPException(400, "as_of must be a date like 2026-09-30")

    @app.get("/api/funds/{fid}/economics")
    def fund_economics(fid: int, as_of: str | None = None, acc=Depends(get_access),
                       conn=Depends(get_conn)):
        return economics.compute(conn, visible_fund(conn, acc, fid), check_date(as_of))

    def activity_for(conn, commitment_id, as_of):
        return [(r["event_date"], r["event_type"], r["amount"] if r["event_type"] == "Capital call" else -r["amount"])
                for r in conn.execute(
                    "SELECT e.event_date, e.event_type, a.amount FROM capital_event_allocations a "
                    "JOIN capital_events e ON e.id = a.event_id WHERE a.commitment_id = ? "
                    "AND e.event_date <= ? ORDER BY e.event_date, e.id", (commitment_id, as_of))]

    @app.get("/reports/statement/{cid}", response_class=HTMLResponse)
    def lp_statement(cid: int, as_of: str | None = None, acc=Depends(get_access),
                     conn=Depends(get_conn)):
        c = d.fetch(conn, "commitments", cid)
        fund = visible_fund(conn, acc, c["fund_id"])
        eco = economics.compute(conn, fund, check_date(as_of))
        lp = next((x for x in eco["lps"] if x["commitment_id"] == cid), None)
        if lp is None:
            raise HTTPException(400, "Statements are for closed commitments")
        body = statement_html(fund, eco, lp, activity_for(conn, cid, eco["as_of"]))
        return PAGE.format(title=escape(f"Statement - {lp['lp']}"), body=body)

    @app.get("/reports/fund/{fid}/statements", response_class=HTMLResponse)
    def all_statements(fid: int, as_of: str | None = None, acc=Depends(get_access),
                       conn=Depends(get_conn)):
        fund = visible_fund(conn, acc, fid)
        eco = economics.compute(conn, fund, check_date(as_of))
        pages = "".join(statement_html(fund, eco, lp, activity_for(conn, lp["commitment_id"], eco["as_of"]))
                        for lp in eco["lps"]) or "<div class='page'>No closed commitments.</div>"
        return PAGE.format(title=escape(f"Statements - {fund['name']}"), body=pages)

    @app.get("/reports/notice/{aid}", response_class=HTMLResponse)
    def notice(aid: int, acc=Depends(get_access), conn=Depends(get_conn)):
        alloc = d.fetch(conn, "capital_event_allocations", aid)
        event = d.fetch(conn, "capital_events", alloc["event_id"])
        fund = visible_fund(conn, acc, event["fund_id"])
        lp = dict(conn.execute("SELECT c.committed_amount, l.name AS lp FROM commitments c "
                               "JOIN lps l ON l.id = c.lp_id WHERE c.id = ?",
                               (alloc["commitment_id"],)).fetchone())
        totals = {"called": 0.0, "distributed": 0.0}
        for r in conn.execute(
                "SELECT e.event_type, a.amount FROM capital_event_allocations a JOIN capital_events e "
                "ON e.id = a.event_id WHERE a.commitment_id = ? AND (e.event_date < ? OR "
                "(e.event_date = ? AND e.id <= ?))",
                (alloc["commitment_id"], event["event_date"], event["event_date"], event["id"])):
            totals["called" if r["event_type"] == "Capital call" else "distributed"] += r["amount"]
        return PAGE.format(title=escape(f"Notice - {lp['lp']}"),
                           body=notice_html(fund, event, alloc, lp, totals))
