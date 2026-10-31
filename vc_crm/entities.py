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

INVESTOR_TYPES = [
    "VC fund", "Corporate VC", "Growth fund", "Angel", "Angel group", "Family office",
    "Accelerator", "Strategic", "Other",
]
ROUND_NAMES = [
    "Pre-seed", "Seed", "Seed extension", "Series A", "Series A extension", "Series B",
    "Series C", "Series D+", "Bridge", "Other",
]
ROUND_STATUSES = ["Planning", "Raising", "Closed", "Abandoned"]
PRO_RATA_DECISIONS = ["Undecided", "Taking full", "Taking partial", "Passing"]
# Ordered left to right on a round's investor pipeline board.
ROUND_INVESTOR_STAGES = [
    "Target", "Intro made", "Pitched", "Diligence", "Term sheet", "Committed", "Passed",
]
PARTICIPATION = ["Lead", "Co-lead", "Participant"]
# Records an activity-log entry can be attached to.
ACTIVITY_PARENTS = ["lps", "commitments", "companies", "funds", "investors", "rounds"]
# Records people, documents and tasks can be attached to.
CONTACT_PARENTS = ["lps", "investors", "companies"]
DOC_PARENTS = ["lps", "commitments", "companies", "funds", "investors", "rounds", "capital_events"]
TASK_PARENTS = ["lps", "commitments", "companies", "funds", "investors", "rounds"]
DOC_TYPES = [
    "Pitch deck", "Memo / IC memo", "Term sheet", "Financials", "Board materials", "LPA",
    "Side letter", "Subscription docs", "Capital call notice", "Quarterly report", "Legal", "Other",
]


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
            f("round_id", "Linked round", "ref", ref="rounds"),
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
    "investors": {
        "label": "Investors",
        "title": "name",
        "order": "name",
        "fields": [
            f("name", "Investor / firm", required=True),
            f("investor_type", "Type", "select", options=INVESTOR_TYPES),
            f("contact_name", "Primary contact"),
            f("email", "Email"),
            f("phone", "Phone"),
            f("website", "Website"),
            f("location", "Location"),
            f("stage_focus", "Stage focus"),
            f("check_size_min", "Check size from", "money"),
            f("check_size_max", "Check size to", "money"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "rounds": {
        "label": "Rounds",
        "title": "round_name",
        "order": "close_date DESC NULLS FIRST, id DESC",
        "fields": [
            f("company_id", "Company", "ref", ref="companies", required=True),
            f("round_name", "Round", "select", options=ROUND_NAMES, required=True),
            f("status", "Status", "select", options=ROUND_STATUSES, default="Planning"),
            f("target_size", "Target size", "money"),
            f("final_size", "Final size", "money"),
            f("pre_money", "Pre-money valuation", "money"),
            f("close_date", "Close date", "date"),
            f("lead_investor_id", "Lead investor", "ref", ref="investors"),
            f("pro_rata_amount", "Our pro-rata right", "money"),
            f("pro_rata_decision", "Pro-rata decision", "select", options=PRO_RATA_DECISIONS),
            f("notes", "Notes", "longtext"),
        ],
    },
    "round_investors": {
        "label": "Round investors",
        "title": "investor_id",
        "order": "id",
        "board": "stage",
        "fields": [
            f("round_id", "Round", "ref", ref="rounds", required=True),
            f("investor_id", "Investor", "ref", ref="investors", required=True),
            f("stage", "Stage", "select", options=ROUND_INVESTOR_STAGES, default="Target"),
            f("participation", "Role", "select", options=PARTICIPATION),
            f("amount", "Amount", "money"),
            f("intro_by", "Intro by"),
            f("owner", "Owner"),
            f("next_step", "Next step"),
            f("next_step_date", "Next step date", "date"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "contacts": {
        "label": "People",
        "title": "name",
        "order": "name",
        "fields": [
            f("name", "Name", required=True),
            f("title", "Title"),
            f("email", "Email"),
            f("phone", "Phone"),
            f("linkedin", "LinkedIn"),
            f("location", "Location"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "contact_links": {
        "label": "Person links",
        "title": "role",
        "order": "id",
        "fields": [
            f("contact_id", "Person", "ref", ref="contacts", required=True),
            f("entity", "Linked to (type)", "select", options=CONTACT_PARENTS, required=True),
            f("entity_id", "Linked record", "integer", required=True),
            f("role", "Role"),
            f("is_primary", "Primary contact", "select", options=["Yes", "No"], default="No"),
        ],
    },
    "documents": {
        "label": "Documents",
        "title": "title",
        "order": "doc_date DESC NULLS LAST, id DESC",
        "fields": [
            f("entity", "Record type", "select", options=DOC_PARENTS, required=True),
            f("entity_id", "Record", "integer", required=True),
            f("title", "Title", required=True),
            f("url", "Link (Drive, Dropbox, DocSend...)", required=True),
            f("doc_type", "Type", "select", options=DOC_TYPES),
            f("doc_date", "Date", "date"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "tasks": {
        "label": "Tasks",
        "title": "title",
        "order": "CASE WHEN status = 'Open' THEN 0 ELSE 1 END, due_date NULLS LAST, id",
        "fields": [
            f("title", "Task", required=True),
            f("due_date", "Due", "date"),
            f("assignee_id", "Assigned to", "ref", ref="users"),
            f("status", "Status", "select", options=["Open", "Done"], default="Open"),
            f("priority", "Priority", "select", options=["Normal", "High"], default="Normal"),
            f("entity", "Related record type", "select", options=TASK_PARENTS),
            f("entity_id", "Related record", "integer"),
            f("notes", "Notes", "longtext"),
        ],
    },
    "activities": {
        "label": "Activity",
        "title": "summary",
        "order": "activity_date DESC, id DESC",
        "fields": [
            f("entity", "Record type", "select",
              options=ACTIVITY_PARENTS, required=True),
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
