-- M3.2 (open-price inflation). trade_outcomes.copyable (set by analytics/outcomes.py) plus the aggregate
-- tables written by analytics/open_inflation.py. Runners ignore "duplicate column name".
ALTER TABLE trade_outcomes ADD COLUMN copyable INTEGER;

CREATE TABLE IF NOT EXISTS open_inflation_stats (
    group_type  TEXT NOT NULL,
    group_key   TEXT NOT NULL,
    k           INTEGER NOT NULL,
    n           INTEGER NOT NULL,
    n_trades    INTEGER NOT NULL,
    mean        REAL,
    median      REAL,
    share_pos   REAL,
    shrunk_mean REAL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (group_type, group_key, k)
);

CREATE TABLE IF NOT EXISTS entry_delays (
    group_type  TEXT NOT NULL,
    group_key   TEXT NOT NULL,
    n           INTEGER NOT NULL,
    n_trades    INTEGER NOT NULL,
    best_k      INTEGER,
    gain        REAL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (group_type, group_key)
);
