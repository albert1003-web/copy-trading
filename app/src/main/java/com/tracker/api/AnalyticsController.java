package com.tracker.api;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import com.tracker.repo.AnalyticsRepository;

/** Analytics views (M3.4): leaderboard by horizon, outcomes, open inflation and the trade detail. Read-only. */
@RestController
@RequestMapping("/api")
public class AnalyticsController {

    private final AnalyticsRepository analytics;

    public AnalyticsController(AnalyticsRepository analytics) {
        this.analytics = analytics;
    }

    @GetMapping("/leaderboard")
    public List<Map<String, Object>> leaderboard(@RequestParam(defaultValue = "20") int horizon) {
        return analytics.leaderboard(checkHorizon(horizon));
    }

    @GetMapping("/outcomes/summary")
    public List<Map<String, Object>> outcomeSummary(@RequestParam(defaultValue = "") String member) {
        return analytics.outcomeSummary(member.trim());
    }

    @GetMapping("/outcomes/members")
    public List<Map<String, Object>> outcomeMembers() {
        return analytics.outcomeMembers();
    }

    @GetMapping("/outcomes/trades")
    public List<Map<String, Object>> outcomeTrades(@RequestParam(defaultValue = "") String member,
                                                   @RequestParam(defaultValue = "100") int limit) {
        return analytics.outcomeTrades(member.trim(), Math.max(1, Math.min(limit, 500)));
    }

    @GetMapping("/open-inflation")
    public Map<String, Object> openInflation() {
        return analytics.openInflation();
    }

    @GetMapping("/trades/{id}")
    public Map<String, Object> trade(@PathVariable long id) {
        return analytics.trade(id).orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "No such trade"));
    }

    /** The trade's symbol vs SPY around the trade. Empty bars when the trade has no symbol. */
    @GetMapping("/trades/{id}/prices")
    public Map<String, Object> tradePrices(@PathVariable long id) {
        Map<String, Object> trade = trade(id);
        Object symbol = trade.get("symbol");
        String disclosed = (String) (trade.get("d0_date") != null ? trade.get("d0_date") : trade.get("disclosure_date"));
        String from = earliest((String) trade.get("tx_date"), disclosed);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("symbol", symbol);
        result.put("tx_date", trade.get("tx_date"));
        result.put("d0_date", trade.get("d0_date"));
        result.put("bars", symbol == null || from == null ? List.of() : analytics.tradePrices((String) symbol, from, disclosed));
        return result;
    }

    private static String earliest(String a, String b) {
        if (a == null) return b;
        if (b == null) return a;
        return a.compareTo(b) <= 0 ? a : b;
    }

    private static int checkHorizon(int horizon) {
        if (!AnalyticsRepository.HORIZONS.contains(horizon)) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "horizon must be one of " + AnalyticsRepository.HORIZONS);
        }
        return horizon;
    }
}
