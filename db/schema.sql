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
    search_seen_at    TEXT,
    available_at      TEXT,                 -- when it counts as public for D0 (UTC): first_seen_at if seen live,
    available_basis   TEXT NOT NULL DEFAULT 'seen'  -- else after the close on filing_date. seen | filed (estimated)
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
    ticker_status      TEXT,                -- enrichment: listed | renamed | unlisted | delisted | none
    is_etf             INTEGER NOT NULL DEFAULT 0,
    sector             TEXT,                -- enrichment: from securities (ETF for funds)
    industry           TEXT,
    mcap_bucket        TEXT                 -- enrichment: mega | large | mid | small | micro, at disclosure
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

-- Price history status per symbol (prices.fetch): ok | partial | missing.
CREATE TABLE IF NOT EXISTS price_coverage (
    symbol      TEXT PRIMARY KEY,
    needed_from TEXT,                       -- earliest date the symbol's trades need
    first_date  TEXT,
    last_date   TEXT,
    n_rows      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    note        TEXT
);

-- Security reference data (enrich/securities.py, from Yahoo): sector, industry, size.
CREATE TABLE IF NOT EXISTS securities (
    symbol             TEXT PRIMARY KEY,
    name               TEXT,
    quote_type         TEXT,                -- EQUITY | ETF | ...
    sector             TEXT,
    industry           TEXT,
    market_cap         REAL,
    shares_outstanding REAL,
    status             TEXT NOT NULL,       -- ok | missing
    updated_at         TEXT NOT NULL
);

-- Committee assignments per Congress (enrich/committees.py); parent committees only.
CREATE TABLE IF NOT EXISTS committee_memberships (
    member_id      TEXT NOT NULL,           -- bioguide id (may predate our members table)
    congress       INTEGER NOT NULL,
    committee_id   TEXT NOT NULL,           -- thomas_id, e.g. HSAS
    committee_name TEXT,
    role           TEXT,
    PRIMARY KEY (member_id, congress, committee_id)
);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    trade_id    INTEGER PRIMARY KEY REFERENCES trades(trade_id),
    d0_date     TEXT,
    d0_open     REAL,
    ret_1 REAL, ret_5 REAL, ret_10 REAL, ret_20 REAL, ret_60 REAL,
    abn_ret_1 REAL, abn_ret_5 REAL, abn_ret_10 REAL, abn_ret_20 REAL, abn_ret_60 REAL,
    open_infl_1 REAL, open_infl_2 REAL, open_infl_3 REAL, open_infl_5 REAL,
    complete    INTEGER NOT NULL DEFAULT 0,
    win_1 INTEGER, win_5 INTEGER, win_10 INTEGER, win_20 INTEGER, win_60 INTEGER,  -- abn > 0; BUYs only (not puts)
    tx_ret      REAL,                       -- context only: trade-date close -> D0 open (never a signal)
    tx_abn_ret  REAL,
    computed_at TEXT,
    copyable    INTEGER                     -- 1 = a BUY we could copy (stock/other or bought calls), else 0
);

-- Open inflation aggregates over copyable BUYs (analytics/open_inflation.py), replaced each night.
CREATE TABLE IF NOT EXISTS open_inflation_stats (
    group_type  TEXT NOT NULL,              -- all | member | mcap | attention
    group_key   TEXT NOT NULL,
    k           INTEGER NOT NULL,           -- 1 | 2 | 3 | 5
    n           INTEGER NOT NULL,           -- filings (one observation each: the mean over its trades)
    n_trades    INTEGER NOT NULL,
    mean        REAL,
    median      REAL,
    share_pos   REAL,                       -- share of filings where buying k days later was cheaper
    shrunk_mean REAL,                       -- empirical Bayes, toward the group type's overall mean
    computed_at TEXT NOT NULL,
    PRIMARY KEY (group_type, group_key, k)
);

-- Best entry delay per group: 0 = buy at the D0 open; NULL = fewer filings than the minimum sample.
CREATE TABLE IF NOT EXISTS entry_delays (
    group_type  TEXT NOT NULL,
    group_key   TEXT NOT NULL,
    n           INTEGER NOT NULL,           -- filings
    n_trades    INTEGER NOT NULL,
    best_k      INTEGER,
    gain        REAL,                       -- shrunk mean open inflation at best_k (0 when best_k = 0)
    computed_at TEXT NOT NULL,
    PRIMARY KEY (group_type, group_key)
);

-- Walk-forward exit validation (analytics/exits.py), replaced each night. One row per book and test quarter, plus
-- an 'all' row per book (test_window = the whole span). Out-of-sample only: walk_forward trades each quarter with the
-- rule chosen on the 2 years before it; hold_h holds h trading days throughout. Stats are per filing.
CREATE TABLE IF NOT EXISTS exit_backtests (
    run_id       INTEGER PRIMARY KEY,
    rule         TEXT NOT NULL,             -- the rule used (walk_forward 'all' rows: walk_forward)
    params       TEXT,                      -- JSON
    train_window TEXT,                      -- YYYY-MM-DD..YYYY-MM-DD (walk_forward quarters only)
    test_window  TEXT,
    mean_abn_ret REAL,                      -- mean over filings of the net excess vs SPY (every test signal)
    hit_rate     REAL,                      -- share of filings with net excess > 0
    max_drawdown REAL,
    n_trades     INTEGER,
    book         TEXT,                      -- walk_forward | hold_1 | hold_5 | hold_10 | hold_20 | hold_60
    universe     TEXT,                      -- ranked | all | watchlist | members
    n_filings    INTEGER,
    mean_ret     REAL,                      -- mean over filings of the net return
    total_return REAL,                      -- portfolio (T+1 ledger) return over the window
    spy_return   REAL,                      -- SPY over the same days
    skipped_cash INTEGER,                   -- signals skipped for lack of settled cash
    computed_at  TEXT
);

-- Nightly leaderboard snapshot (analytics/leaderboard.py): copyable BUYs from D0, one observation per filing,
-- ranked at `horizon` (20 trading days) by shrunk_score. rank is NULL below the minimum sample.
CREATE TABLE IF NOT EXISTS member_scores (
    member_id      TEXT NOT NULL REFERENCES members(member_id),
    as_of          TEXT NOT NULL,
    n_trades       INTEGER,
    mean_abn_ret   REAL,                    -- mean_ret - mean_spy_ret: excess over the S&P 500
    hit_rate       REAL,                    -- share of filings that beat SPY
    shrunk_score   REAL,
    rank           INTEGER,
    horizon        INTEGER,
    n_filings      INTEGER,
    mean_ret       REAL,                    -- average return of the member's buys
    mean_spy_ret   REAL,                    -- SPY's average return over the same windows
    median_abn_ret REAL,
    consistency    REAL,                    -- share of years (>= 3 filings) with a positive mean excess
    PRIMARY KEY (member_id, as_of)
);

-- v2 alert score inputs (analytics/factors.py), replaced each night: average 20-day excess vs SPY of copyable
-- buys per trade feature level (common/signals.py), one observation per filing, empirical-Bayes shrunk.
CREATE TABLE IF NOT EXISTS signal_factors (
    factor      TEXT NOT NULL,              -- all | mcap | delay | amount | committee | kind
    level       TEXT NOT NULL,
    n           INTEGER NOT NULL,           -- filings
    n_trades    INTEGER NOT NULL,
    mean        REAL,                       -- clipped mean excess (as the score uses it)
    shrunk      REAL,
    effect      REAL,                       -- shrunk - the factor's pooled mean: what the score adds
    computed_at TEXT NOT NULL,
    PRIMARY KEY (factor, level)
);

-- The same stats for every horizon (h = 1/5/10/20/60), per snapshot.
CREATE TABLE IF NOT EXISTS member_horizon_stats (
    member_id      TEXT NOT NULL REFERENCES members(member_id),
    as_of          TEXT NOT NULL,
    horizon        INTEGER NOT NULL,
    n_filings      INTEGER NOT NULL,
    n_trades       INTEGER NOT NULL,
    mean_ret       REAL,
    mean_spy_ret   REAL,
    mean_abn_ret   REAL,
    median_abn_ret REAL,
    hit_rate       REAL,
    shrunk_score   REAL,
    PRIMARY KEY (member_id, as_of, horizon)
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
-- idx_filings_available (filings(available_at)) is likewise created by migration 005 only.
CREATE INDEX IF NOT EXISTS idx_filings_first_seen ON filings(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at);
