# VC CRM

A CRM for venture capital partners covering both sides of the business:

1. **Fundraising**: LPs, the LP pipeline per fund, commitments, capital calls and distributions.
2. **Investments**: the deal pipeline, portfolio holdings, valuation marks, exits and returns.

The UI is modeled on HubSpot's Kanban board view (drag cards between stages) and
Airtable's grid view (click any cell to edit it inline, sort by column, search).

## Run it

```bash
pip install -r requirements.txt
uvicorn vc_crm.main:app --reload
# open http://localhost:8000
```

Data is stored in SQLite at `./data/vc_crm.db`. Set `VC_CRM_DB=/path/to/file.db` to change it.
The database starts empty: no sample data is included.

Run the tests:

```bash
pip install -r requirements-dev.txt
pytest
```

## Screens

| Screen | View style | What it does |
|---|---|---|
| Dashboard | KPI tiles + tables | Commitments, called, distributed, invested, portfolio value, gross MOIC/IRR, fundraising progress per fund, deal funnel counts, next steps due in 14 days or overdue |
| LP pipeline | Board / Grid | One card per LP per fund. Stages: Prospect, Contacted, Meeting, Due diligence, Soft circle, Legal / docs, Closed, Declined. Column headers show count and dollar total. Dropping a card on **Closed** asks for the committed amount. |
| LPs | Grid | LP directory (type, contact, owner, tags) with total committed across funds. The record drawer shows every fund commitment and an activity log. |
| Funds & capital | Grid | Fund list with committed, % of target, called, uncalled, distributed, DPI, and capital called but not yet invested. LP capital accounts (paid-in, unfunded, distributed, DPI per LP). Capital calls and distributions with per-LP allocation and paid/received tracking. |
| Deal pipeline | Board / Grid | One card per company. Stages: Sourced, First meeting, Diligence, IC review, Term sheet, Invested, Passed. Dropping on **Passed** asks for a pass reason. |
| Portfolio | Grid | One row per company per fund: invested, realized, unrealized, total value, MOIC, IRR, valuation basis. |
| All companies | Grid | Every company regardless of stage. The drawer has tabs for investments, valuations and realizations. |

Every LP, commitment, company and fund has an activity log (calls, meetings, emails, notes).

## Data model

```
funds ──< commitments >── lps          (one commitment per LP per fund = LP pipeline card)
funds ──< capital_events ──< capital_event_allocations >── commitments
funds ──< investments / valuations / realizations >── companies   (company = deal pipeline card)
activities  (polymorphic: entity + entity_id)
```

## How the numbers are calculated

- **Committed** = sum of `committed_amount` on commitments in the **Closed** stage.
- **Soft circled** = sum of `target_amount` in **Soft circle** and **Legal / docs**.
- **Capital calls and distributions** are split across the fund's closed commitments
  pro rata to committed capital, rounded to the cent so the parts sum exactly to the total.
  A call cannot exceed the fund's remaining uncalled capital. The split is stored when the
  event is created, so later commitment changes do not rewrite history.
- **LP DPI** = distributions / paid-in capital.
- **Holding value**: the latest valuation mark for that company in that fund, plus any money
  invested after that mark (carried at cost). With no mark, the position is held at cost.
  After a **Full exit** or **Write-off**, unrealized value is zero unless a later mark exists.
- **MOIC** = (realized + unrealized) / invested.
- **IRR** = XIRR on dated cash flows (investments out, proceeds in, current value as a terminal
  flow on today's date).

Portfolio MOIC and IRR are **gross** (before management fees, fund expenses and carry). The
CRM does not model fees or carry, so it does not report net TVPI or net IRR to LPs.

IRR on positions held for less than a year is annualised and can look extreme (for example a
1.8x mark after 8 months shows above 100%). Many funds label sub-12-month IRR "not meaningful".

## API

All screens use a JSON API you can also script against:

- `GET/POST /api/{entity}`, `GET/PATCH/DELETE /api/{entity}/{id}` for
  `funds, lps, commitments, companies, investments, valuations, realizations, activities`.
  List endpoints accept equality filters, e.g. `/api/commitments?fund_id=1&stage=Closed`.
- `GET/POST /api/capital-events`, `GET /api/capital-events/{id}/allocations`,
  `DELETE /api/capital-events/{id}`, `PATCH /api/allocations/{id}` (`{"paid": true}`).
- `GET /api/dashboard`, `/api/portfolio?fund_id=`, `/api/funds/summary`, `/api/lp-positions?fund_id=`.
- `GET /api/meta` returns field definitions and stage lists (edit `vc_crm/entities.py` to
  change stages, LP types or fields; add matching columns in `vc_crm/schema.sql`).

## Not included in v1

- Authentication and multi-user permissions (run it on a trusted network or behind a proxy with auth).
- Email/calendar sync, document storage, LP reporting PDFs.
- Fee, carry and waterfall modeling (net returns).
- Schema migrations: the schema is created on first run; changing columns later needs a manual migration.
