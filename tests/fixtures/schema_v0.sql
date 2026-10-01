-- Congressional Trade Tracker schema (design doc §5).
-- Portable SQL: runs on SQLite now, Postgres later. Safe to re-run.

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
    doc_id        TEXT PRIMARY KEY,
    member_id     TEXT REFERENCES members(member_id),
    chamber       TEXT NOT NULL,
    filing_date   TEXT,                     -- ISO date
    source_url    TEXT,
    raw_path      TEXT,
    first_seen_at TEXT NOT NULL,            -- ISO timestamp (UTC)
    parse_status  TEXT NOT NULL DEFAULT 'pending'  -- pending | parsed | needs_review | failed
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
    confidence         REAL NOT NULL DEFAULT 1.0
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
    suggested_exit  TEXT
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

CREATE INDEX IF NOT EXISTS idx_trades_member ON trades(member_id);
CREATE INDEX IF NOT EXISTS idx_trades_disclosure ON trades(disclosure_date);
CREATE INDEX IF NOT EXISTS idx_filings_first_seen ON filings(first_seen_at);
