-- Congressional Trade Tracker schema (design doc §5).
-- Portable SQL: runs on SQLite now, Postgres later. Safe to re-run.
--
-- This file is always the FULL current schema (fresh databases are built from it).
-- Every change here also needs a migration in db/migrations/ to upgrade existing databases.
-- Both the Python pipelines (db/__init__.py) and the desktop app (MigrationRunner) apply it.

CREATE TABLE IF NOT EXISTS members (
    member_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    chamber     TEXT NOT NULL,              -- house | senate
    party       TEXT,
    state       TEXT,
    committees  TEXT,                       -- JSON array of committee names
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS filings (
    doc_id            TEXT PRIMARY KEY,
    member_id         TEXT REFERENCES members(member_id),  -- NULL until enrichment resolves the filer
    chamber           TEXT NOT NULL,
    filing_date       TEXT,                 -- ISO date
    source_url        TEXT,
    raw_path          TEXT,
    first_seen_at     TEXT NOT NULL,        -- ISO timestamp (UTC)
    parse_status      TEXT NOT NULL DEFAULT 'pending',  -- pending | parsed | needs_review | failed
    filer_name        TEXT,                 -- as listed by the source, e.g. "Hon. Nancy Pelosi"
    state_district    TEXT,                 -- House: e.g. CA11
    filing_year       INTEGER,
    doc_format        TEXT,                 -- electronic | scanned
    first_seen_source TEXT,                 -- which source listed it first: index | search
    index_seen_at     TEXT,                 -- when each source first listed it (detection lag)
    search_seen_at    TEXT
);

-- HTTP caching state per polled source (ETag / Last-Modified for conditional GETs).
CREATE TABLE IF NOT EXISTS source_state (
    source        TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    checked_at    TEXT,
    changed_at    TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id           INTEGER PRIMARY KEY,
    doc_id             TEXT NOT NULL REFERENCES filings(doc_id),
    member_id          TEXT REFERENCES members(member_id),
    ticker             TEXT,
    asset_type         TEXT,                -- stock | option | other
    action             TEXT,                -- BUY | SELL | SELL_PARTIAL | EXCHANGE
    owner              TEXT,                -- self | spouse | joint | dependent
    tx_date            TEXT,
    disclosure_date    TEXT,
    amount_min         INTEGER,
    amount_max         INTEGER,
    filing_delay_days  INTEGER,
    committee_relevant INTEGER NOT NULL DEFAULT 0,
    confidence         REAL NOT NULL DEFAULT 1.0,
    line_no            INTEGER,             -- row position within the filing; (doc_id, line_no) is the upsert key
    asset_name         TEXT,                -- asset text as filed, e.g. "Apple Inc. - Common Stock (AAPL)"
    asset_code         TEXT,                -- House code (ST, OP, GS, ...) or Senate "Asset Type"
    description        TEXT,                -- House Description/Comments or Senate Comment
    symbol             TEXT,                -- enrichment: validated symbol to trade/price (NULL if none)
    ticker_status      TEXT,                -- enrichment: listed | renamed | unlisted | none
    is_etf             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prices (
    ticker    TEXT NOT NULL,
    date      TEXT NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    adj_close REAL,
    volume    INTEGER,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    trade_id    INTEGER PRIMARY KEY REFERENCES trades(trade_id),
    d0_date     TEXT,
    d0_open     REAL,
    ret_1 REAL, ret_5 REAL, ret_10 REAL, ret_20 REAL, ret_60 REAL,
    abn_ret_1 REAL, abn_ret_5 REAL, abn_ret_10 REAL, abn_ret_20 REAL, abn_ret_60 REAL,
    open_infl_1 REAL, open_infl_2 REAL, open_infl_3 REAL, open_infl_5 REAL,
    complete    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exit_backtests (
    run_id       INTEGER PRIMARY KEY,
    rule         TEXT NOT NULL,
    params       TEXT,                      -- JSON
    train_window TEXT,
    test_window  TEXT,
    mean_abn_ret REAL,
    hit_rate     REAL,
    max_drawdown REAL,
    n_trades     INTEGER
);

CREATE TABLE IF NOT EXISTS member_scores (
    member_id    TEXT NOT NULL REFERENCES members(member_id),
    as_of        TEXT NOT NULL,
    n_trades     INTEGER,
    mean_abn_ret REAL,
    hit_rate     REAL,
    shrunk_score REAL,
    rank         INTEGER,
    PRIMARY KEY (member_id, as_of)
);

CREATE TABLE IF NOT EXISTS watchlist (
    member_id TEXT PRIMARY KEY REFERENCES members(member_id),
    added_at  TEXT NOT NULL,
    reason    TEXT,
    active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id        INTEGER PRIMARY KEY,
    trade_id        INTEGER NOT NULL REFERENCES trades(trade_id),
    sent_at         TEXT NOT NULL,
    score           REAL,
    suggested_entry TEXT,
    suggested_exit  TEXT,
    rule            TEXT                    -- watchlist_buy | held_sale
);

-- One notice per filing that has no trade rows to alert on (e.g. a watched member's scanned filing).
CREATE TABLE IF NOT EXISTS filing_alerts (
    doc_id  TEXT PRIMARY KEY REFERENCES filings(doc_id),
    kind    TEXT NOT NULL,                  -- scanned
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS my_positions (
    position_id INTEGER PRIMARY KEY,
    trade_id    INTEGER REFERENCES trades(trade_id),   -- source signal
    ticker      TEXT NOT NULL,
    buy_date    TEXT NOT NULL,
    buy_price   REAL NOT NULL,
    shares      REAL NOT NULL,
    exit_rule   TEXT,
    sell_date   TEXT,
    sell_price  REAL,
    status      TEXT NOT NULL DEFAULT 'open'           -- open | closed
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id       INTEGER PRIMARY KEY,
    agent        TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    inputs       TEXT,
    tools_called TEXT,
    output       TEXT,
    approved     INTEGER                    -- NULL = pending, 1 = approved, 0 = rejected
);

-- One row per pipeline run (python -m pipeline.run): latency report and the app's Pipeline tab.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id      INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,              -- ISO timestamp (UTC)
    finished_at TEXT,
    status      TEXT NOT NULL,              -- running | ok | failed
    stages      TEXT,                       -- JSON: {stage: {"ok": bool, "summary": {...}, "error": str|null}}
    warnings    TEXT                        -- JSON list of strings
);

CREATE INDEX IF NOT EXISTS idx_trades_member ON trades(member_id);
CREATE INDEX IF NOT EXISTS idx_trades_disclosure ON trades(disclosure_date);
-- idx_trades_doc_line (UNIQUE trades(doc_id, line_no)) is created by migration 002 only: this file runs
-- before migrations, and on an older database trades.line_no doesn't exist yet at that point.
CREATE INDEX IF NOT EXISTS idx_filings_first_seen ON filings(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at);
