package com.tracker.api;

import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.tracker.repo.TradeRepository;

@RestController
@RequestMapping("/api")
public class TradesController {

    private final TradeRepository trades;

    public TradesController(TradeRepository trades) {
        this.trades = trades;
    }

    @GetMapping("/trades")
    public List<Map<String, Object>> trades(@RequestParam(defaultValue = "") String member,
                                            @RequestParam(defaultValue = "") String ticker,
                                            @RequestParam(defaultValue = "") String action,
                                            @RequestParam(defaultValue = "200") int limit) {
        return trades.trades(member.trim(), ticker.trim(), action.trim(), Math.min(limit, 1000));
    }
}
