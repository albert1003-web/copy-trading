package com.tracker.api;

import java.util.List;
import java.util.Map;

import org.springframework.boot.SpringApplication;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.tracker.repo.PipelineRepository;
import com.tracker.repo.TradeRepository;

@RestController
@RequestMapping("/api")
public class SystemController {

    private final TradeRepository trades;
    private final PipelineRepository pipeline;
    private final ConfigurableApplicationContext context;

    public SystemController(TradeRepository trades, PipelineRepository pipeline, ConfigurableApplicationContext context) {
        this.trades = trades;
        this.pipeline = pipeline;
        this.context = context;
    }

    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", "ok");
    }

    @GetMapping("/summary")
    public Map<String, Object> summary() {
        return trades.summary();
    }

    @GetMapping("/pipeline/health")
    public Map<String, Object> pipelineHealth() {
        return pipeline.health();
    }

    @GetMapping("/pipeline/runs")
    public List<Map<String, Object>> pipelineRuns(@RequestParam(defaultValue = "50") int limit) {
        return pipeline.runs(limit);
    }

    @GetMapping("/pipeline/history")
    public Map<String, Object> pipelineHistory() {
        return pipeline.history();
    }

    @GetMapping("/filings/recent")
    public List<Map<String, Object>> recentFilings(@RequestParam(defaultValue = "20") int limit) {
        return trades.recentFilings(limit);
    }

    @GetMapping("/alerts/recent")
    public List<Map<String, Object>> recentAlerts(@RequestParam(defaultValue = "20") int limit) {
        return trades.recentAlerts(limit);
    }

    /** Stops the app (UI "Quit" button). Runs after the response is sent. */
    @PostMapping("/shutdown")
    public Map<String, String> shutdown() {
        Thread quit = new Thread(() -> {
            try {
                Thread.sleep(300);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
            System.exit(SpringApplication.exit(context));
        });
        quit.setDaemon(false);
        quit.start();
        return Map.of("status", "stopping");
    }
}
