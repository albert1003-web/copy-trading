-- M3.5 (v2 scoring). Per-feature 20-day excess of copyable buys, written by analytics/factors.py and read by the
-- alert score.
CREATE TABLE IF NOT EXISTS signal_factors (
    factor      TEXT NOT NULL,
    level       TEXT NOT NULL,
    n           INTEGER NOT NULL,
    n_trades    INTEGER NOT NULL,
    mean        REAL,
    shrunk      REAL,
    effect      REAL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (factor, level)
);
