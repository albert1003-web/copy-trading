-- M5.1 (agent foundation). agent_runs gains run status and usage; agent_proposals holds each run's proposals,
-- approved one by one in the app and carried out by agents/apply.py.
ALTER TABLE agent_runs ADD COLUMN finished_at TEXT;
ALTER TABLE agent_runs ADD COLUMN model TEXT;
ALTER TABLE agent_runs ADD COLUMN status TEXT;
ALTER TABLE agent_runs ADD COLUMN error TEXT;
ALTER TABLE agent_runs ADD COLUMN usage TEXT;
CREATE TABLE IF NOT EXISTS agent_proposals (
    proposal_id  INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES agent_runs(run_id),
    position     INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    member_id    TEXT,
    title        TEXT NOT NULL,
    rationale    TEXT NOT NULL,
    evidence     TEXT,
    approved     INTEGER,
    decided_at   TEXT,
    applied_at   TEXT,
    apply_result TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_proposals_run ON agent_proposals(run_id);
