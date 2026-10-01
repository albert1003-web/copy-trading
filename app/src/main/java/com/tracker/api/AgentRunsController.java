package com.tracker.api;

import java.util.List;
import java.util.Map;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.tracker.repo.AgentRunRepository;

/** Agent outputs and the human approve/reject decision on their proposals. */
@RestController
@RequestMapping("/api/agent-runs")
public class AgentRunsController {

    record Decision(boolean approved) {}

    private final AgentRunRepository runs;

    public AgentRunsController(AgentRunRepository runs) {
        this.runs = runs;
    }

    @GetMapping
    public List<Map<String, Object>> runs(@RequestParam(defaultValue = "50") int limit) {
        return runs.runs(Math.min(limit, 500));
    }

    @PostMapping("/{id}/decision")
    public ResponseEntity<Void> decide(@PathVariable long id, @RequestBody Decision decision) {
        return runs.decide(id, decision.approved()) == 1
                ? ResponseEntity.noContent().build()
                : ResponseEntity.notFound().build();
    }
}
