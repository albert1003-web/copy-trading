-- M4.2 (walk-forward exit validation). One row per book (walk_forward | hold_1 ... hold_60) and test quarter, plus
-- an 'all' row per book, written by analytics/exits.py and replaced each run. Out-of-sample results only.
ALTER TABLE exit_backtests ADD COLUMN book TEXT;
ALTER TABLE exit_backtests ADD COLUMN universe TEXT;
ALTER TABLE exit_backtests ADD COLUMN n_filings INTEGER;
ALTER TABLE exit_backtests ADD COLUMN mean_ret REAL;
ALTER TABLE exit_backtests ADD COLUMN total_return REAL;
ALTER TABLE exit_backtests ADD COLUMN spy_return REAL;
ALTER TABLE exit_backtests ADD COLUMN skipped_cash INTEGER;
ALTER TABLE exit_backtests ADD COLUMN computed_at TEXT;
