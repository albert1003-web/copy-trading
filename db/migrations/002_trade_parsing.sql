-- M1.3 parsing: each trade's position within its filing (the upsert key) and the source's asset text.
-- Runners ignore "duplicate column name", so this is safe on databases built from the current schema.sql.
ALTER TABLE trades ADD COLUMN line_no INTEGER;
ALTER TABLE trades ADD COLUMN asset_name TEXT;
ALTER TABLE trades ADD COLUMN asset_code TEXT;
ALTER TABLE trades ADD COLUMN description TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_doc_line ON trades(doc_id, line_no);
