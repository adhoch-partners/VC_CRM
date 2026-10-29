PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- Access

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    active        INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Only a hash of the cookie token is stored, so a database leak cannot be replayed.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id INTEGER,
    changes   TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- Fundraising

CREATE TABLE IF NOT EXISTS funds (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    vintage          INTEGER,
    target_size      REAL,
    hard_cap         REAL,
    status           TEXT NOT NULL DEFAULT 'Raising',
    first_close_date TEXT,
    final_close_date TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lps (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    lp_type            TEXT,
    contact_name       TEXT,
    email              TEXT,
    phone              TEXT,
    location           TEXT,
    relationship_owner TEXT,
    tags               TEXT,
    notes              TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-fund permissions for non-admin users. No row = no access to that fund.
CREATE TABLE IF NOT EXISTS fund_access (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fund_id INTEGER NOT NULL REFERENCES funds(id) ON DELETE CASCADE,
    level   TEXT NOT NULL CHECK (level IN ('viewer', 'editor')),
    PRIMARY KEY (user_id, fund_id)
);

-- One row per LP per fund: this is the card on the fundraising board.
CREATE TABLE IF NOT EXISTS commitments (
    id               INTEGER PRIMARY KEY,
    lp_id            INTEGER NOT NULL REFERENCES lps(id) ON DELETE CASCADE,
    fund_id          INTEGER NOT NULL REFERENCES funds(id) ON DELETE RESTRICT,
    stage            TEXT NOT NULL DEFAULT 'Prospect',
    target_amount    REAL,
    committed_amount REAL,
    close_date       TEXT,
    owner            TEXT,
    next_step        TEXT,
    next_step_date   TEXT,
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lp_id, fund_id)
);

-- Capital calls and distributions at the fund level ...
CREATE TABLE IF NOT EXISTS capital_events (
    id           INTEGER PRIMARY KEY,
    fund_id      INTEGER NOT NULL REFERENCES funds(id) ON DELETE RESTRICT,
    event_type   TEXT NOT NULL CHECK (event_type IN ('Capital call', 'Distribution')),
    event_date   TEXT NOT NULL,
    due_date     TEXT,
    total_amount REAL NOT NULL CHECK (total_amount > 0),
    purpose      TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ... split pro rata across closed commitments at the time of the event.
CREATE TABLE IF NOT EXISTS capital_event_allocations (
    id            INTEGER PRIMARY KEY,
    event_id      INTEGER NOT NULL REFERENCES capital_events(id) ON DELETE CASCADE,
    commitment_id INTEGER NOT NULL REFERENCES commitments(id) ON DELETE RESTRICT,
    amount        REAL NOT NULL,
    paid          INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------- Investments

-- A company is a card on the deal board; once invested it is a portfolio holding.
CREATE TABLE IF NOT EXISTS companies (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    website        TEXT,
    sector         TEXT,
    company_stage  TEXT,
    location       TEXT,
    founders       TEXT,
    description    TEXT,
    source         TEXT,
    deal_stage     TEXT NOT NULL DEFAULT 'Sourced',
    deal_owner     TEXT,
    fund_id        INTEGER REFERENCES funds(id) ON DELETE SET NULL,
    round_size     REAL,
    proposed_check REAL,
    pre_money      REAL,
    next_step      TEXT,
    next_step_date TEXT,
    pass_reason    TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Outside funds and angels: a shared directory, like LPs.
CREATE TABLE IF NOT EXISTS investors (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    investor_type  TEXT,
    contact_name   TEXT,
    email          TEXT,
    phone          TEXT,
    website        TEXT,
    location       TEXT,
    stage_focus    TEXT,
    check_size_min REAL,
    check_size_max REAL,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A financing round of a portfolio company (ours or anyone's money).
CREATE TABLE IF NOT EXISTS rounds (
    id                INTEGER PRIMARY KEY,
    company_id        INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    round_name        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'Planning',
    target_size       REAL,
    final_size        REAL,
    pre_money         REAL,
    close_date        TEXT,
    lead_investor_id  INTEGER REFERENCES investors(id) ON DELETE SET NULL,
    pro_rata_amount   REAL,
    pro_rata_decision TEXT,
    notes             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per outside investor per round: the card on a round's pipeline board.
CREATE TABLE IF NOT EXISTS round_investors (
    id             INTEGER PRIMARY KEY,
    round_id       INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    investor_id    INTEGER NOT NULL REFERENCES investors(id) ON DELETE RESTRICT,
    stage          TEXT NOT NULL DEFAULT 'Target',
    participation  TEXT,
    amount         REAL,
    intro_by       TEXT,
    owner          TEXT,
    next_step      TEXT,
    next_step_date TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (round_id, investor_id)
);

CREATE TABLE IF NOT EXISTS investments (
    id                   INTEGER PRIMARY KEY,
    company_id           INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    fund_id              INTEGER NOT NULL REFERENCES funds(id) ON DELETE RESTRICT,
    investment_date      TEXT NOT NULL,
    round_name           TEXT,
    instrument           TEXT,
    round_id             INTEGER REFERENCES rounds(id) ON DELETE SET NULL,
    amount               REAL NOT NULL CHECK (amount > 0),
    shares               REAL,
    price_per_share      REAL,
    post_money_valuation REAL,
    ownership_pct        REAL,
    notes                TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Fair value of the fund's whole position in a company as of a date.
CREATE TABLE IF NOT EXISTS valuations (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    fund_id        INTEGER NOT NULL REFERENCES funds(id) ON DELETE RESTRICT,
    valuation_date TEXT NOT NULL,
    fair_value     REAL NOT NULL CHECK (fair_value >= 0),
    method         TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS realizations (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    fund_id          INTEGER NOT NULL REFERENCES funds(id) ON DELETE RESTRICT,
    realization_date TEXT NOT NULL,
    realization_type TEXT NOT NULL,
    proceeds         REAL NOT NULL DEFAULT 0 CHECK (proceeds >= 0),
    notes            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- Shared

CREATE TABLE IF NOT EXISTS activities (
    id            INTEGER PRIMARY KEY,
    entity        TEXT NOT NULL,
    entity_id     INTEGER NOT NULL,
    activity_date TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'Note',
    summary       TEXT NOT NULL,
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_commitments_fund ON commitments(fund_id);
CREATE INDEX IF NOT EXISTS idx_alloc_event ON capital_event_allocations(event_id);
CREATE INDEX IF NOT EXISTS idx_alloc_commitment ON capital_event_allocations(commitment_id);
CREATE INDEX IF NOT EXISTS idx_investments_company ON investments(company_id);
CREATE INDEX IF NOT EXISTS idx_valuations_company ON valuations(company_id);
CREATE INDEX IF NOT EXISTS idx_realizations_company ON realizations(company_id);
CREATE INDEX IF NOT EXISTS idx_activities_entity ON activities(entity, entity_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_rounds_company ON rounds(company_id);
CREATE INDEX IF NOT EXISTS idx_round_investors_round ON round_investors(round_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);
