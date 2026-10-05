package com.tracker.repo;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * Agent run log and proposals (written by agents/runner.py). The user only sets approval decisions; approved
 * proposals are carried out later by the pipeline (agents/apply.py), which sets applied_at.
 */
@Repository
public class AgentRunRepository {

    public enum Decision { DECIDED, NOT_FOUND, ALREADY_APPLIED }

    private final JdbcTemplate jdbc;
    private final ObjectMapper json;

    public AgentRunRepository(JdbcTemplate jdbc, ObjectMapper json) {
        this.jdbc = jdbc;
        this.json = json;
    }

    /** Newest runs first, each with its proposals (in the agent's order) under "proposals". */
    public List<Map<String, Object>> runs(int limit) {
        List<Map<String, Object>> runs = jdbc.queryForList("""
                SELECT run_id, agent, started_at, finished_at, model, status, error, output, approved
                FROM agent_runs
                ORDER BY started_at DESC
                LIMIT ?
                """, limit);
        if (runs.isEmpty()) {
            return runs;
        }
        List<Object> ids = runs.stream().map(r -> r.get("run_id")).toList();
        String marks = String.join(", ", Collections.nCopies(ids.size(), "?"));
        Map<Long, List<Map<String, Object>>> byRun = new HashMap<>();
        for (Map<String, Object> p : jdbc.queryForList("""
                SELECT p.proposal_id, p.run_id, p.kind, p.member_id, m.name AS member_name, p.title, p.rationale,
                       p.evidence, p.approved, p.decided_at, p.applied_at, p.apply_result
                FROM agent_proposals p
                LEFT JOIN members m ON m.member_id = p.member_id
                WHERE p.run_id IN (%s)
                ORDER BY p.run_id, p.position
                """.formatted(marks), ids.toArray())) {
            p.put("evidence", parseList((String) p.get("evidence")));
            byRun.computeIfAbsent(((Number) p.remove("run_id")).longValue(), k -> new ArrayList<>()).add(p);
        }
        for (Map<String, Object> r : runs) {
            r.put("proposals", byRun.getOrDefault(((Number) r.get("run_id")).longValue(), List.of()));
        }
        return runs;
    }

    public int decide(long runId, boolean approved) {
        return jdbc.update("UPDATE agent_runs SET approved = ? WHERE run_id = ?", approved ? 1 : 0, runId);
    }

    /** A decision can change until the pipeline has applied the proposal. */
    public Decision decideProposal(long proposalId, boolean approved, String decidedAt) {
        int updated = jdbc.update(
                "UPDATE agent_proposals SET approved = ?, decided_at = ? WHERE proposal_id = ? AND applied_at IS NULL",
                approved ? 1 : 0, decidedAt, proposalId);
        if (updated == 1) {
            return Decision.DECIDED;
        }
        Integer exists = jdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_proposals WHERE proposal_id = ?", Integer.class, proposalId);
        return exists != null && exists > 0 ? Decision.ALREADY_APPLIED : Decision.NOT_FOUND;
    }

    private List<?> parseList(String text) {
        if (text == null || text.isBlank()) {
            return List.of();
        }
        try {
            return json.readValue(text, List.class);
        } catch (JsonProcessingException e) {
            return List.of(Map.of("claim", text, "source", ""));
        }
    }
}
