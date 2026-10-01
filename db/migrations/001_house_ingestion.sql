-- M1.1 House ingestion: raw filer details, document format, and per-source detection timestamps.
-- Runners ignore "duplicate column name", so this is safe on databases built from the current schema.sql.
ALTER TABLE filings ADD COLUMN filer_name TEXT;
ALTER TABLE filings ADD COLUMN state_district TEXT;
ALTER TABLE filings ADD COLUMN filing_year INTEGER;
ALTER TABLE filings ADD COLUMN doc_format TEXT;
ALTER TABLE filings ADD COLUMN first_seen_source TEXT;
ALTER TABLE filings ADD COLUMN index_seen_at TEXT;
ALTER TABLE filings ADD COLUMN search_seen_at TEXT;

CREATE TABLE IF NOT EXISTS source_state (
    source        TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    checked_at    TEXT,
    changed_at    TEXT
);
