package com.tracker.api;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.tracker.repo.AgentRunRepository;

/**
 * Agent outputs and the human approve/reject decisions: one per proposal, or one per run for runs without
 * proposals. Approving only records the decision; the pipeline applies approved watchlist proposals.
 */
@RestController
@RequestMapping("/api")
public class AgentRunsController {

    record Decision(boolean approved) {}

    private final AgentRunRepository runs;

    public AgentRunsController(AgentRunRepository runs) {
        this.runs = runs;
    }

    @GetMapping("/agent-runs")
    public List<Map<String, Object>> runs(@RequestParam(defaultValue = "50") int limit) {
        return runs.runs(Math.min(limit, 500));
    }

    @PostMapping("/agent-runs/{id}/decision")
    public ResponseEntity<Void> decide(@PathVariable long id, @RequestBody Decision decision) {
        return runs.decide(id, decision.approved()) == 1
                ? ResponseEntity.noContent().build()
                : ResponseEntity.notFound().build();
    }

    @PostMapping("/agent-proposals/{id}/decision")
    public ResponseEntity<?> decideProposal(@PathVariable long id, @RequestBody Decision decision) {
        return switch (runs.decideProposal(id, decision.approved(), Instant.now().toString())) {
            case DECIDED -> ResponseEntity.noContent().build();
            case NOT_FOUND -> ResponseEntity.notFound().build();
            case ALREADY_APPLIED -> ResponseEntity.status(HttpStatus.CONFLICT)
                    .body(Map.of("error", "Already applied; the decision can't change"));
        };
    }
}
