# VC CRM

A CRM for venture capital partners covering both sides of the business:

1. **Fundraising**: LPs, the LP pipeline per fund, commitments, capital calls and distributions.
2. **Investments**: the deal pipeline, portfolio holdings, valuation marks, exits and returns.

The UI is modeled on HubSpot's Kanban board view (drag cards between stages) and
Airtable's grid view (click any cell to edit it inline, sort by column, search).

It is a multi-user web app: people sign in with email and password, and each user is an
**admin** (manages users, edits everything), **editor** (creates, edits, deletes records) or
**viewer** (read-only). Every change is recorded with who made it.

## Deploy to Render (hosted, multi-user)

The repo includes a `Dockerfile` and a `render.yaml` Blueprint that creates a web service plus a
managed Postgres database that only the web service can reach.

1. Create an account at render.com and connect it to the GitHub organization that owns this repo.
2. In Render, choose **New > Blueprint**, pick this repository and the branch to deploy.
3. Render asks for three values. These create the first admin on first start:
   - `ADMIN_NAME`: your name
   - `ADMIN_EMAIL`: your sign-in email
   - `ADMIN_PASSWORD`: at least 10 characters. Use a password manager.
4. Click **Apply**. When the deploy is green, open the `onrender.com` URL and sign in.
5. Go to **Admin > Users > + Invite user** to add your team. Share each temporary password
   privately; people change it under their name at the bottom of the sidebar.
6. Optional: under the web service's **Settings > Custom Domains**, add something like
   `crm.yourfirm.com`.

After the first admin exists, `ADMIN_EMAIL` / `ADMIN_PASSWORD` are ignored, so you can delete
them from the service's environment.

**Cost (check current pricing):** the Blueprint uses the `starter` web plan and the
`basic-256mb` Postgres plan, which together have been roughly $13/month. Do not switch the
database to Render's free plan: free databases expire after a trial period and have no backups.
The `render.yaml` was written without access to Render's docs from the build environment; if
Render rejects a field, the error will name it.

**Backups:** paid Render Postgres plans include automatic backups / point-in-time recovery
(retention depends on plan). For an extra copy you control, run `pg_dump` against the database's
external URL on a schedule.

**Other hosts:** the `Dockerfile` runs anywhere that runs containers (Fly.io, Railway, AWS, Azure,
GCP). Set `DATABASE_URL` to a Postgres connection string, `COOKIE_SECURE=true`, and serve it over
HTTPS. The container listens on `$PORT` (default 8000).

## Run it locally

```bash
pip install -r requirements.txt
ADMIN_EMAIL=you@firm.com ADMIN_PASSWORD='a long password' uvicorn vc_crm.main:app --reload
# open http://localhost:8000
```

Without `DATABASE_URL`, data is stored in SQLite at `./data/vc_crm.db` (`VC_CRM_DB` changes the
path). The database starts empty: no sample data is included.

Run the tests (add `TEST_DATABASE_URL=postgresql://...` pointing at a throwaway database to also
run every API test against Postgres; its `public` schema is wiped before each test):

```bash
pip install -r requirements-dev.txt
pytest
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | unset | Postgres connection string. When unset, SQLite is used. |
| `VC_CRM_DB` | `./data/vc_crm.db` | SQLite file path (local use). |
| `COOKIE_SECURE` | `false` | Set `true` whenever served over HTTPS (always in production). |
| `SESSION_DAYS` | `7` | How long a sign-in lasts. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` | unset | Create the first admin, only when no users exist. |

## Managing users

- **In the app (admins):** Admin > Users to invite, change role, reset a password, or deactivate.
  Deactivating or resetting a password signs that person out everywhere immediately. Users are
  deactivated rather than deleted so the change history keeps their name. The app will not let
  you remove the last active admin.
- **From a server shell** (e.g. Render's Shell tab), useful if every admin is locked out:

  ```bash
  python -m vc_crm.manage list-users
  python -m vc_crm.manage create-user --email you@firm.com --name "Your Name" --role admin
  python -m vc_crm.manage reset-password --email you@firm.com
  ```

## Security model

- Passwords are hashed with scrypt; minimum 10 characters.
- Sign-in sets an HttpOnly, SameSite=Lax cookie (Secure in production). The database stores
  only a SHA-256 of the session token, so sessions can be revoked and a database copy cannot be
  used to sign in.
- Every API call checks the session and the user's current role, so role changes and
  deactivations take effect on the next click.
- Writes require an `X-CRM` request header, which other websites cannot send, to block
  cross-site request forgery.
- After 10 failed sign-ins in 15 minutes for an email (or client IP), further attempts are
  refused for the rest of that window. This is in memory, so it resets when the app restarts.
  Someone who knows a user's email can deliberately trigger this lockout for that user.
- Security headers: Content-Security-Policy, X-Frame-Options DENY, nosniff, HSTS (production),
  and `Cache-Control: no-store` on all data responses. The interactive API docs are disabled.
- The Postgres database in the Blueprint accepts connections only from your Render services.
- Not included: two-factor authentication, single sign-on, password-reset emails, or per-fund
  permissions (every signed-in user can see all funds and deals). Ask if you need these.

## Screens

| Screen | View style | What it does |
|---|---|---|
| Dashboard | KPI tiles + tables | Commitments, called, distributed, invested, portfolio value, gross MOIC/IRR, fundraising progress per fund, deal funnel counts, next steps due in 14 days or overdue |
| LP pipeline | Board / Grid | One card per LP per fund. Stages: Prospect, Contacted, Meeting, Due diligence, Soft circle, Legal / docs, Closed, Declined. Column headers show count and dollar total. Dropping a card on **Closed** asks for the committed amount. |
| LPs | Grid | LP directory (type, contact, owner, tags) with total committed across funds. The record drawer shows every fund commitment and an activity log. |
| Funds & capital | Grid | Fund list with committed, % of target, called, uncalled, distributed, DPI, and capital called but not yet invested. LP capital accounts (paid-in, unfunded, distributed, DPI per LP). Capital calls and distributions with per-LP allocation and paid/received tracking. |
| Deal pipeline | Board / Grid | One card per company. Stages: Sourced, First meeting, Diligence, IC review, Term sheet, Invested, Passed. Dropping on **Passed** asks for a pass reason. |
| Portfolio | Grid | One row per company per fund: invested, realized, unrealized, total value, MOIC, IRR, valuation basis. |
| Users (admins only) | Grid | Invite users, set roles, reset passwords, deactivate. |
| All companies | Grid | Every company regardless of stage. The drawer has tabs for investments, valuations and realizations. |

Every LP, commitment, company and fund has an activity log (calls, meetings, emails, notes, with
who logged each one) and a change history showing who changed which field, from what, to what.
Viewers see the same screens with editing turned off.

## Data model

```
funds ──< commitments >── lps          (one commitment per LP per fund = LP pipeline card)
funds ──< capital_events ──< capital_event_allocations >── commitments
funds ──< investments / valuations / realizations >── companies   (company = deal pipeline card)
activities  (polymorphic: entity + entity_id, created_by -> users)
users ──< sessions          audit_log (who, what, when, field-level changes)
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

All screens use a JSON API. Every endpoint except `POST /api/auth/login` and `GET /healthz`
needs a signed-in session cookie, and writes need the `X-CRM: 1` header.

- `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/password`.
- Admins: `GET/POST /api/users`, `PATCH /api/users/{id}` (`name`, `role`, `active`, `password`).
- `GET /api/audit?entity=lps&entity_id=1` for change history.

- `GET/POST /api/{entity}`, `GET/PATCH/DELETE /api/{entity}/{id}` for
  `funds, lps, commitments, companies, investments, valuations, realizations, activities`.
  List endpoints accept equality filters, e.g. `/api/commitments?fund_id=1&stage=Closed`.
- `GET/POST /api/capital-events`, `GET /api/capital-events/{id}/allocations`,
  `DELETE /api/capital-events/{id}`, `PATCH /api/allocations/{id}` (`{"paid": true}`).
- `GET /api/dashboard`, `/api/portfolio?fund_id=`, `/api/funds/summary`, `/api/lp-positions?fund_id=`.
- `GET /api/meta` returns field definitions and stage lists (edit `vc_crm/entities.py` to
  change stages, LP types or fields; add matching columns in `vc_crm/schema.sql`).

## Not included yet

- Email/calendar sync, document storage, LP reporting PDFs.
- Fee, carry and waterfall modeling (net returns).
- Full schema migrations: new tables and the columns listed in `vc_crm/db.py` (`ADDED_COLUMNS`)
  are added automatically on startup; renaming or retyping columns needs a manual migration.
