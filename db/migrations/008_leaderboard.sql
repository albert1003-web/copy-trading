-- M3.3 (member leaderboard). member_scores gains the horizon, sample sizes, average return vs the S&P 500 and
-- consistency; member_horizon_stats holds every horizon. Runners ignore "duplicate column name".
ALTER TABLE member_scores ADD COLUMN horizon INTEGER;
ALTER TABLE member_scores ADD COLUMN n_filings INTEGER;
ALTER TABLE member_scores ADD COLUMN mean_ret REAL;
ALTER TABLE member_scores ADD COLUMN mean_spy_ret REAL;
ALTER TABLE member_scores ADD COLUMN median_abn_ret REAL;
ALTER TABLE member_scores ADD COLUMN consistency REAL;

CREATE TABLE IF NOT EXISTS member_horizon_stats (
    member_id      TEXT NOT NULL REFERENCES members(member_id),
    as_of          TEXT NOT NULL,
    horizon        INTEGER NOT NULL,
    n_filings      INTEGER NOT NULL,
    n_trades       INTEGER NOT NULL,
    mean_ret       REAL,
    mean_spy_ret   REAL,
    mean_abn_ret   REAL,
    median_abn_ret REAL,
    hit_rate       REAL,
    shrunk_score   REAL,
    PRIMARY KEY (member_id, as_of, horizon)
);
