package com.tracker.repo;

import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Our own manually executed trades (user-owned). */
@Repository
public class PositionRepository {

    private static final String BENCHMARK = "SPY";
    private static final double SPLIT_TOLERANCE = 0.4;  // alerts/positions.py

    private final JdbcTemplate jdbc;

    public PositionRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * Positions with their exit email, if the watcher (alerts/positions.py) has sent one, plus P&L. Prices are the
     * stored closes (split-adjusted, dividends not added): the same basis as the price you paid, as in the watcher
     * and the daily digest. Open positions are valued at the latest close, closed ones at your sell price.
     */
    public List<Map<String, Object>> positions() {
        List<Map<String, Object>> rows = jdbc.queryForList("""
                SELECT p.*, e.triggered_on, e.reason AS exit_reason
                FROM my_positions p LEFT JOIN exit_alerts e ON e.position_id = p.position_id
                ORDER BY p.status = 'closed', p.buy_date DESC, p.position_id DESC
                """);
        rows.forEach(this::addPnl);
        return rows;
    }

    private void addPnl(Map<String, Object> p) {
        String ticker = ((String) p.get("ticker")).toUpperCase();
        String buyDate = (String) p.get("buy_date");
        double buy = ((Number) p.get("buy_price")).doubleValue();
        double shares = ((Number) p.get("shares")).doubleValue();
        boolean open = "open".equals(p.get("status"));

        Map<String, Object> first = bar(ticker, "date >= ?", "date", buyDate);
        // A buy-day close far from your price means a split since then moved every stored price (as in the watcher).
        if (first != null && Math.abs(num(first, "close") / buy - 1) > SPLIT_TOLERANCE) {
            p.put("price_note", "stored prices are far from your buy price (a split?): re-log it at the adjusted price");
            first = null;
        }
        String endDate;
        Double price;
        if (open) {
            Map<String, Object> last = first == null ? null : bar(ticker, "date >= ?", "date DESC", buyDate);
            endDate = last == null ? null : (String) last.get("date");
            price = last == null ? null : num(last, "close");
            p.put("last_close", price);
            p.put("last_date", endDate);
        } else {
            endDate = (String) p.get("sell_date");
            price = p.get("sell_price") == null ? null : ((Number) p.get("sell_price")).doubleValue();
        }
        if (price != null) {
            p.put("market_value", price * shares);
            p.put("pnl", (price - buy) * shares);
            p.put("ret", price / buy - 1);
            Map<String, Object> spyBuy = bar(BENCHMARK, "date <= ?", "date DESC", buyDate);
            Map<String, Object> spyEnd = endDate == null ? null : bar(BENCHMARK, "date <= ?", "date DESC", endDate);
            if (spyBuy != null && spyEnd != null) {
                double spyRet = num(spyEnd, "close") / num(spyBuy, "close") - 1;
                p.put("spy_ret", spyRet);
                p.put("excess", price / buy - 1 - spyRet);
            }
        }
        ExitRuleLabel rule = ExitRuleLabel.parse((String) p.get("exit_rule"));
        if (open && rule != null && p.get("triggered_on") == null) {
            addRuleStatus(p, rule, ticker, buyDate, buy, first);
        }
    }

    /**
     * Where a watched rule stands: days held vs its max hold, and the stop/target the next check uses
     * (common/exit_rules.levels). The trailing high starts at max(your price, the buy-day close), then takes each
     * later day's high, as alerts/positions.py simulates a position bought intraday.
     */
    private void addRuleStatus(Map<String, Object> p, ExitRuleLabel rule, String ticker, String buyDate, double buy,
                               Map<String, Object> first) {
        p.put("days_held", jdbc.queryForObject(
                "SELECT COUNT(*) FROM prices WHERE ticker = ? AND date > ?", Integer.class, BENCHMARK, buyDate));
        p.put("max_hold", rule.maxHold());
        Double stop = null;
        Double target = null;
        switch (rule.name()) {
            case "stop_target" -> {
                Double s = rule.param("stop");
                Double t = rule.param("target");
                stop = s == null ? null : buy * (1 - s);
                target = t == null ? null : buy * (1 + t);
            }
            case "trailing_stop" -> {
                Double high = first == null ? null : trailingHigh(ticker, buy, first);
                stop = high == null ? null : high * (1 - rule.param("pct"));
            }
            case "atr_stop" -> {
                Double high = first == null ? null : trailingHigh(ticker, buy, first);
                Double atr = first == null ? null : atrBefore(ticker, (String) first.get("date"), rule.param("n").intValue());
                stop = high == null || atr == null ? null : high - rule.param("mult") * atr;
            }
            default -> { }
        }
        p.put("stop_level", stop);
        p.put("target_level", target);
    }

    private double trailingHigh(String ticker, double buy, Map<String, Object> first) {
        Double later = jdbc.queryForObject(
                "SELECT MAX(high) FROM prices WHERE ticker = ? AND date > ?", Double.class, ticker, first.get("date"));
        return Math.max(Math.max(buy, num(first, "close")), later == null ? 0 : later);
    }

    /** Average true range over the n bars before the buy day (common/exit_rules.atr_before), or null. */
    private Double atrBefore(String ticker, String buyDay, int n) {
        List<Map<String, Object>> bars = jdbc.queryForList("""
                SELECT high, low, close FROM prices
                WHERE ticker = ? AND date < ? AND close IS NOT NULL AND high IS NOT NULL
                ORDER BY date DESC LIMIT ?
                """, ticker, buyDay, n + 1);
        if (bars.size() < n + 1) {
            return null;
        }
        double sum = 0;
        for (int i = 0; i < n; i++) {  // newest first: bar i's previous close is bar i + 1's
            Map<String, Object> bar = bars.get(i);
            double prevClose = num(bars.get(i + 1), "close");
            sum += Math.max(num(bar, "high"), prevClose) - Math.min(num(bar, "low"), prevClose);
        }
        return sum / n;
    }

    /** The first bar with a close matching {@code where}, in {@code order}, or null. */
    private Map<String, Object> bar(String ticker, String where, String order, String date) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT date, close FROM prices WHERE ticker = ? AND close IS NOT NULL AND " + where
                        + " ORDER BY " + order + " LIMIT 1", ticker, date);
        return rows.isEmpty() ? null : rows.get(0);
    }

    private static double num(Map<String, Object> row, String key) {
        return ((Number) row.get(key)).doubleValue();
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
