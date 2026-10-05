package com.tracker.api;

import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import com.tracker.repo.PositionRepository;

/** The exit rules a logged position can follow, with the current recommendation. Read-only. */
@RestController
public class ExitRulesController {

    private final PositionRepository positions;

    public ExitRulesController(PositionRepository positions) {
        this.positions = positions;
    }

    @GetMapping("/api/exit-rules")
    public List<Map<String, Object>> exitRules() {
        return positions.exitRules();
    }
}
