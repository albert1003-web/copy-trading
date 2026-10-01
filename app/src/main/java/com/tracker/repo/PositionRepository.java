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

    public List<Map<String, Object>> positions() {
        return jdbc.queryForList("""
                SELECT * FROM my_positions
                ORDER BY status = 'closed', buy_date DESC, position_id DESC
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
