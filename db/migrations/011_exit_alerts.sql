-- M4.3 (exits in alerts). exit_rules: the tested exit rules with the current recommendation, written by
-- analytics/exits.py and read by the alerts stage and the app. exit_alerts: one exit email per logged position.
CREATE TABLE IF NOT EXISTS exit_rules (
    label        TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    position     INTEGER NOT NULL,
    train_score  REAL,
    recommended  INTEGER NOT NULL DEFAULT 0,
    confidence   TEXT,
    reason       TEXT,
    train_window TEXT,
    computed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exit_alerts (
    position_id  INTEGER PRIMARY KEY REFERENCES my_positions(position_id),
    rule         TEXT NOT NULL,
    triggered_on TEXT NOT NULL,
    reason       TEXT NOT NULL,
    price        REAL,
    sent_at      TEXT NOT NULL
);
