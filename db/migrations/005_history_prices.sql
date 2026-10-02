-- M2 (history & prices).
-- available_at: when a filing counts as public for D0. For live detections it's first_seen_at. For backfilled
-- filings (and those loaded before scheduling started) it's estimated as after the close on filing_date.
-- Runners ignore "duplicate column name", so this is safe on databases built from the current schema.sql.
ALTER TABLE filings ADD COLUMN available_at TEXT;
ALTER TABLE filings ADD COLUMN available_basis TEXT NOT NULL DEFAULT 'seen';
ALTER TABLE trades ADD COLUMN sector TEXT;
ALTER TABLE trades ADD COLUMN industry TEXT;
ALTER TABLE trades ADD COLUMN mcap_bucket TEXT;

UPDATE filings SET available_at = first_seen_at WHERE available_at IS NULL;

-- Filings loaded before the first scheduled run weren't detected live: estimate from the filing date.
-- 21:00Z is after the 4 pm ET close in both EST and EDT, so D0 is the next trading-day open.
UPDATE filings SET available_at = filing_date || 'T21:00:00Z', available_basis = 'filed'
WHERE filing_date IS NOT NULL
  AND first_seen_at < (SELECT MIN(started_at) FROM pipeline_runs)
  AND filing_date || 'T21:00:00Z' < first_seen_at;

CREATE TABLE IF NOT EXISTS price_coverage (
    symbol      TEXT PRIMARY KEY,
    needed_from TEXT,
    first_date  TEXT,
    last_date   TEXT,
    n_rows      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS securities (
    symbol             TEXT PRIMARY KEY,
    name               TEXT,
    quote_type         TEXT,
    sector             TEXT,
    industry           TEXT,
    market_cap         REAL,
    shares_outstanding REAL,
    status             TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS committee_memberships (
    member_id      TEXT NOT NULL,
    congress       INTEGER NOT NULL,
    committee_id   TEXT NOT NULL,
    committee_name TEXT,
    role           TEXT,
    PRIMARY KEY (member_id, congress, committee_id)
);

CREATE INDEX IF NOT EXISTS idx_filings_available ON filings(available_at);
