package com.tracker.repo;

import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Our own manually executed trades (user-owned). */
@Repository
public class PositionRepository {

    private final JdbcTemplate jdbc;

    public PositionRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** Positions with their exit email, if the watcher (alerts/positions.py) has sent one. */
    public List<Map<String, Object>> positions() {
        return jdbc.queryForList("""
                SELECT p.*, e.triggered_on, e.reason AS exit_reason
                FROM my_positions p LEFT JOIN exit_alerts e ON e.position_id = p.position_id
                ORDER BY p.status = 'closed', p.buy_date DESC, p.position_id DESC
                """);
    }

    /** The tested exit rules (analytics/exits.py), grid order; one is recommended. Read-only. */
    public List<Map<String, Object>> exitRules() {
        return jdbc.queryForList("""
                SELECT label, description, recommended, confidence, reason, train_score, train_window
                FROM exit_rules ORDER BY position
                """);
    }

    public void open(Long tradeId, String ticker, String buyDate, double buyPrice, double shares, String exitRule) {
        jdbc.update("""
                INSERT INTO my_positions (trade_id, ticker, buy_date, buy_price, shares, exit_rule, status)
                VALUES (?, UPPER(?), ?, ?, ?, ?, 'open')
                """, tradeId, ticker, buyDate, buyPrice, shares, exitRule);
    }

    public int close(long positionId, String sellDate, double sellPrice) {
        return jdbc.update("""
                UPDATE my_positions SET sell_date = ?, sell_price = ?, status = 'closed'
                WHERE position_id = ? AND status = 'open'
                """, sellDate, sellPrice, positionId);
    }
}
