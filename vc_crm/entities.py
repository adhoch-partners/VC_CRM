"""Field metadata for every editable table.

This is the single source of truth for validation on the server and for the
grid columns, board cards and edit forms rendered by the browser UI.
"""

FUND_STATUSES = ["Raising", "Investing", "Harvesting", "Closed"]

LP_TYPES = [
    "Family office", "Endowment", "Foundation", "Pension", "Fund of funds",
    "Sovereign wealth", "Insurance", "Corporate", "Individual / HNWI", "Other",
]

# Ordered left to right on the fundraising board.
LP_STAGES = [
    "Prospect", "Contacted", "Meeting", "Due diligence", "Soft circle",
    "Legal / docs", "Closed", "Declined",
]

# Ordered left to right on the deal board.
DEAL_STAGES = [
    "Sourced", "First meeting", "Diligence", "IC review", "Term sheet",
    "Invested", "Passed",
]

COMPANY_STAGES = ["Pre-seed", "Seed", "Series A", "Series B", "Series C", "Growth", "Other"]
INSTRUMENTS = ["Equity", "SAFE", "Convertible note", "Warrant", "Other"]
VALUATION_METHODS = [
    "Last round", "Cost", "Comparable companies", "DCF", "Write-down", "Other",
]
REALIZATION_TYPES = ["Full exit", "Partial exit", "Dividend", "Write-off"]
ACTIVITY_KINDS = ["Note", "Call", "Meeting", "Email"]


def f(name, label, type_="text", **kw):
    return {"name": name, "label": label, "type": type_, **kw}


ENTITIES = {
    "funds": {
        "label": "Funds",
        "title": "name",
        "order": "vintage DESC NULLS LAST, name",
        "fields": [
            f("name", "Fund name", required=True),
            f("vintage", "Vintage", "integer"),
            f("target_size", "Target size", "money"),
            f("hard_cap", "Hard cap", "money"),
            f("status", "Status", "select", options=FUND_STATUSES, default="Raising"),
            f("first_close_date", "First close", "date"),
            f("final_close_date", "Final close", "date"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "lps": {
        "label": "LPs",
        "title": "name",
        "order": "name",
        "fields": [
            f("name", "LP name", required=True),
            f("lp_type", "Type", "select", options=LP_TYPES),
            f("contact_name", "Primary contact"),
            f("email", "Email"),
            f("phone", "Phone"),
            f("location", "Location"),
            f("relationship_owner", "Relationship owner"),
            f("tags", "Tags"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "commitments": {
        "label": "LP pipeline",
        "title": "lp_id",
        "order": "id",
        "board": "stage",
        "fields": [
            f("lp_id", "LP", "ref", ref="lps", required=True),
            f("fund_id", "Fund", "ref", ref="funds", required=True),
            f("stage", "Stage", "select", options=LP_STAGES, default="Prospect"),
            f("target_amount", "Target ask", "money"),
            f("committed_amount", "Committed", "money"),
            f("close_date", "Close date", "date"),
            f("owner", "Owner"),
            f("next_step", "Next step"),
            f("next_step_date", "Next step date", "date"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "companies": {
        "label": "Companies",
        "title": "name",
        "order": "name",
        "board": "deal_stage",
        "fields": [
            f("name", "Company", required=True),
            f("deal_stage", "Deal stage", "select", options=DEAL_STAGES, default="Sourced"),
            f("fund_id", "Fund", "ref", ref="funds"),
            f("sector", "Sector"),
            f("company_stage", "Company stage", "select", options=COMPANY_STAGES),
            f("website", "Website"),
            f("location", "Location"),
            f("founders", "Founders"),
            f("source", "Source / referrer"),
            f("deal_owner", "Deal owner"),
            f("round_size", "Round size", "money"),
            f("proposed_check", "Proposed check", "money"),
            f("pre_money", "Pre-money valuation", "money"),
            f("next_step", "Next step"),
            f("next_step_date", "Next step date", "date"),
            f("pass_reason", "Pass reason"),
            f("description", "Description", "longtext"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "investments": {
        "label": "Investments",
        "title": "round_name",
        "order": "investment_date DESC",
        "fields": [
            f("company_id", "Company", "ref", ref="companies", required=True),
            f("fund_id", "Fund", "ref", ref="funds", required=True),
            f("investment_date", "Date", "date", required=True),
            f("round_name", "Round"),
            f("instrument", "Instrument", "select", options=INSTRUMENTS),
            f("amount", "Amount invested", "money", required=True),
            f("shares", "Shares", "number"),
            f("price_per_share", "Price / share", "number"),
            f("post_money_valuation", "Post-money valuation", "money"),
            f("ownership_pct", "Ownership %", "percent"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "valuations": {
        "label": "Valuations",
        "title": "valuation_date",
        "order": "valuation_date DESC",
        "fields": [
            f("company_id", "Company", "ref", ref="companies", required=True),
            f("fund_id", "Fund", "ref", ref="funds", required=True),
            f("valuation_date", "Date", "date", required=True),
            f("fair_value", "Fair value of position", "money", required=True),
            f("method", "Method", "select", options=VALUATION_METHODS),
            f("notes", "Notes", "longtext"),
        ],
    },
    "realizations": {
        "label": "Realizations",
        "title": "realization_date",
        "order": "realization_date DESC",
        "fields": [
            f("company_id", "Company", "ref", ref="companies", required=True),
            f("fund_id", "Fund", "ref", ref="funds", required=True),
            f("realization_date", "Date", "date", required=True),
            f("realization_type", "Type", "select", options=REALIZATION_TYPES, required=True),
            f("proceeds", "Proceeds", "money"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "activities": {
        "label": "Activity",
        "title": "summary",
        "order": "activity_date DESC, id DESC",
        "fields": [
            f("entity", "Record type", "select",
              options=["lps", "commitments", "companies", "funds"], required=True),
            f("entity_id", "Record", "integer", required=True),
            f("activity_date", "Date", "date", required=True),
            f("kind", "Kind", "select", options=ACTIVITY_KINDS, default="Note"),
            f("summary", "Summary", "longtext", required=True),
        ],
    },
}

# Capital events are created through a dedicated endpoint (allocation logic),
# but share the same metadata format for the UI form.
CAPITAL_EVENT_FIELDS = [
    f("fund_id", "Fund", "ref", ref="funds", required=True),
    f("event_type", "Type", "select", options=["Capital call", "Distribution"], required=True),
    f("event_date", "Notice date", "date", required=True),
    f("due_date", "Due date", "date"),
    f("total_amount", "Total amount", "money", required=True),
    f("purpose", "Purpose"),
    f("notes", "Notes", "longtext"),
]
