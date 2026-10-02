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

    /** History and prices (M2): price coverage, the parse/review queue by chamber and year, the last nightly run. */
    public Map<String, Object> history() {
        Map<String, Object> history = new LinkedHashMap<>();
        Map<String, Object> coverage = new LinkedHashMap<>();
        for (String status : List.of("ok", "partial", "missing")) {
            coverage.put(status, 0);
        }
        jdbc.queryForList("SELECT status, COUNT(*) AS n FROM price_coverage GROUP BY status")
                .forEach(r -> coverage.put(String.valueOf(r.get("status")), r.get("n")));
        history.put("price_coverage", coverage);
        history.put("trades_with_symbol", jdbc.queryForObject("""
                SELECT COUNT(*) FROM trades WHERE ticker_status IN ('listed', 'renamed', 'unlisted')
                """, Integer.class));
        history.put("trades_priced", jdbc.queryForObject("""
                SELECT COUNT(*) FROM trades t JOIN price_coverage c ON c.symbol = t.symbol WHERE c.status = 'ok'
                """, Integer.class));
        history.put("review_queue", jdbc.queryForList("""
                SELECT f.chamber, f.filing_year AS year, COUNT(*) AS filings,
                       SUM(f.parse_status = 'parsed') AS parsed,
                       SUM(f.parse_status = 'needs_review' AND f.doc_format = 'scanned') AS scanned,
                       SUM(f.parse_status = 'needs_review' AND f.doc_format <> 'scanned') AS needs_review,
                       SUM(f.parse_status = 'failed') AS failed,
                       SUM(f.parse_status = 'pending') AS pending
                FROM filings f GROUP BY f.chamber, f.filing_year ORDER BY f.filing_year DESC, f.chamber
                """));
        List<String> nightly = jdbc.queryForList(
                "SELECT checked_at FROM source_state WHERE source = 'pipeline.nightly'", String.class);
        history.put("last_nightly", nightly.isEmpty() ? null : nightly.get(0));
        return history;
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
