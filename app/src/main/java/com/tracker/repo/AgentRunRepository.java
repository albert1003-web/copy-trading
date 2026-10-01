package com.tracker.repo;

import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Agent run log (written by agents); the user only sets the approval decision. */
@Repository
public class AgentRunRepository {

    private final JdbcTemplate jdbc;

    public AgentRunRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public List<Map<String, Object>> runs(int limit) {
        return jdbc.queryForList("""
                SELECT run_id, agent, started_at, output, approved
                FROM agent_runs
                ORDER BY started_at DESC
                LIMIT ?
                """, limit);
    }

    public int decide(long runId, boolean approved) {
        return jdbc.update("UPDATE agent_runs SET approved = ? WHERE run_id = ?", approved ? 1 : 0, runId);
    }
}
