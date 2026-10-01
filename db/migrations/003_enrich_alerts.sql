-- M1.4 enrichment: the validated symbol downstream code uses, and why a trade has none.
-- M1.5 alerts: which rule fired, and one-per-filing notices (scanned filings have no trade rows).
-- Runners ignore "duplicate column name", so this is safe on databases built from the current schema.sql.
ALTER TABLE trades ADD COLUMN symbol TEXT;
ALTER TABLE trades ADD COLUMN ticker_status TEXT;
ALTER TABLE trades ADD COLUMN is_etf INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alerts ADD COLUMN rule TEXT;

CREATE TABLE IF NOT EXISTS filing_alerts (
    doc_id  TEXT PRIMARY KEY REFERENCES filings(doc_id),
    kind    TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
