-- M3.1 (trade outcomes). Win labels per horizon (BUYs only), the pre-disclosure move (context only), and when
-- the row was computed. Runners ignore "duplicate column name", so this is safe on databases built from schema.sql.
ALTER TABLE trade_outcomes ADD COLUMN win_1 INTEGER;
ALTER TABLE trade_outcomes ADD COLUMN win_5 INTEGER;
ALTER TABLE trade_outcomes ADD COLUMN win_10 INTEGER;
ALTER TABLE trade_outcomes ADD COLUMN win_20 INTEGER;
ALTER TABLE trade_outcomes ADD COLUMN win_60 INTEGER;
ALTER TABLE trade_outcomes ADD COLUMN tx_ret REAL;
ALTER TABLE trade_outcomes ADD COLUMN tx_abn_ret REAL;
ALTER TABLE trade_outcomes ADD COLUMN computed_at TEXT;
