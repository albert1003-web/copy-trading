-- M1.6 scheduling: one row per scheduled (or manual) pipeline run, for the detection-latency report
-- and the app's Pipeline tab.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id      INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    stages      TEXT,
    warnings    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs(started_at);
