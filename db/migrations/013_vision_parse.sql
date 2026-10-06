-- M5.4 (filing parser fallback). How each filing's trades were read: text (the PDF/HTML parsers) or vision
-- (Claude reading a scanned filing, parse/llm_fallback.py). Vision trades stay out of analytics.
ALTER TABLE filings ADD COLUMN parse_method TEXT;
ALTER TABLE filings ADD COLUMN vision_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE filings ADD COLUMN vision_attempted_at TEXT;
ALTER TABLE filings ADD COLUMN vision_error TEXT;
UPDATE filings SET parse_method = 'text' WHERE doc_format = 'electronic' AND parse_status IN ('parsed', 'needs_review') AND parse_method IS NULL;
