package com.tracker.api;

import java.util.List;
import java.util.Map;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.tracker.repo.PositionRepository;

/** Log of our own trades. Recording only: nothing here places orders. */
@RestController
@RequestMapping("/api/positions")
public class PositionsController {

    record OpenRequest(Long tradeId, String ticker, String buyDate, Double buyPrice, Double shares, String exitRule) {}

    record CloseRequest(String sellDate, Double sellPrice) {}

    private final PositionRepository positions;

    public PositionsController(PositionRepository positions) {
        this.positions = positions;
    }

    @GetMapping
    public List<Map<String, Object>> positions() {
        return positions.positions();
    }

    @PostMapping
    public ResponseEntity<Void> open(@RequestBody OpenRequest req) {
        if (isBlank(req.ticker()) || isBlank(req.buyDate()) || req.buyPrice() == null || req.shares() == null
                || req.buyPrice() <= 0 || req.shares() <= 0) {
            return ResponseEntity.badRequest().build();
        }
        positions.open(req.tradeId(), req.ticker().trim(), req.buyDate(), req.buyPrice(), req.shares(), req.exitRule());
        return ResponseEntity.noContent().build();
    }

    @PostMapping("/{id}/close")
    public ResponseEntity<Void> close(@PathVariable long id, @RequestBody CloseRequest req) {
        if (isBlank(req.sellDate()) || req.sellPrice() == null || req.sellPrice() <= 0) {
            return ResponseEntity.badRequest().build();
        }
        return positions.close(id, req.sellDate(), req.sellPrice()) == 1
                ? ResponseEntity.noContent().build()
                : ResponseEntity.notFound().build();
    }

    private static boolean isBlank(String s) {
        return s == null || s.isBlank();
    }
}
