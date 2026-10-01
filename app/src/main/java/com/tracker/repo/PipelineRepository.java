package com.tracker.repo;

import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Read-only pipeline health from pipeline_runs (written by python -m pipeline.run). */
@Repository
public class PipelineRepository {

    private final JdbcTemplate jdbc;

    public PipelineRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** Recent runs, newest first, with their stage summaries and warnings as stored (JSON text). */
    public List<Map<String, Object>> runs(int limit) {
        return jdbc.queryForList("""
                SELECT run_id, started_at, finished_at, status, stages, warnings
                FROM pipeline_runs ORDER BY run_id DESC LIMIT ?
                """, limit);
    }

    /** The latest run, the latest successful one, failed runs since the last success, and runs in the last 24 h. */
    public Map<String, Object> health() {
        List<Map<String, Object>> last = jdbc.queryForList("""
                SELECT run_id, started_at, finished_at, status, stages, warnings
                FROM pipeline_runs ORDER BY run_id DESC LIMIT 1
                """);
        String dayAgo = Instant.now().minus(24, ChronoUnit.HOURS).truncatedTo(ChronoUnit.SECONDS).toString();
        Map<String, Object> health = new LinkedHashMap<>();
        health.put("last_run", last.isEmpty() ? null : last.get(0));
        health.put("last_ok_at", jdbc.queryForObject(
                "SELECT MAX(started_at) FROM pipeline_runs WHERE status = 'ok'", String.class));
        health.put("failure_streak", jdbc.queryForObject("""
                SELECT COUNT(*) FROM pipeline_runs
                WHERE status = 'failed'
                  AND run_id > COALESCE((SELECT MAX(run_id) FROM pipeline_runs WHERE status = 'ok'), 0)
                """, Integer.class));
        health.put("runs_24h", jdbc.queryForObject(
                "SELECT COUNT(*) FROM pipeline_runs WHERE started_at >= ?", Integer.class, dayAgo));
        return health;
    }
}
