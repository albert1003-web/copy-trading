package com.tracker.repo;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * Read-only analytics written by the Python analytics stages: trade outcomes (M3.1), open inflation (M3.2) and
 * the member leaderboard (M3.3). Every number is measured from D0 (the first open after disclosure); averages
 * treat each filing as one observation, as the pipelines do.
 */
@Repository
public class AnalyticsRepository {

    /** Trading-day horizons the outcomes are measured at (analytics/outcomes.py HORIZONS). */
    public static final List<Integer> HORIZONS = List.of(1, 5, 10, 20, 60);
    /** Filings a member needs to be ranked (analytics/leaderboard.py MIN_N). */
    public static final int MIN_FILINGS = 20;

    private final JdbcTemplate jdbc;

    public AnalyticsRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** Latest leaderboard snapshot at one horizon: ranked members (enough filings) first, by shrunk score. */
    public List<Map<String, Object>> leaderboard(int horizon) {
        return jdbc.queryForList("""
                SELECT h.member_id, h.as_of, h.horizon, h.n_filings, h.n_trades, h.mean_ret, h.mean_spy_ret,
                       h.mean_abn_ret, h.median_abn_ret, h.hit_rate, h.shrunk_score, s.consistency,
                       CASE WHEN h.n_filings >= ? THEN ROW_NUMBER() OVER (
                           PARTITION BY h.n_filings >= ? ORDER BY h.shrunk_score DESC, h.mean_abn_ret DESC) END AS rank,
                       m.name, m.chamber, m.party
                FROM member_horizon_stats h
                JOIN members m ON m.member_id = h.member_id
                LEFT JOIN member_scores s ON s.member_id = h.member_id AND s.as_of = h.as_of
                WHERE h.as_of = (SELECT MAX(as_of) FROM member_horizon_stats) AND h.horizon = ?
                ORDER BY rank IS NULL, rank, h.n_filings DESC
                """, MIN_FILINGS, MIN_FILINGS, horizon);
    }

    /** Per horizon, copyable buys (all members, or one): average return, the S&P 500 over the same windows,
     *  the excess and the hit rate, one observation per filing. Horizons with no matured trades are left out. */
    public List<Map<String, Object>> outcomeSummary(String memberId) {
        List<Map<String, Object>> rows = new ArrayList<>();
        for (int h : HORIZONS) {  // h comes from the fixed list above, never from the request
            Map<String, Object> row = jdbc.queryForMap("""
                    SELECT COUNT(*) AS n_filings, SUM(n) AS n_trades, AVG(r) AS mean_ret, AVG(spy) AS mean_spy_ret,
                           AVG(r) - AVG(spy) AS mean_abn_ret, AVG(a > 0) AS hit_rate
                    FROM (SELECT t.doc_id, COUNT(*) AS n, AVG(o.ret_%1$d) AS r,
                                 AVG(o.ret_%1$d - o.abn_ret_%1$d) AS spy, AVG(o.abn_ret_%1$d) AS a
                          FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
                          WHERE o.copyable = 1 AND o.ret_%1$d IS NOT NULL AND o.abn_ret_%1$d IS NOT NULL
                            AND (? = '' OR t.member_id = ?)
                          GROUP BY t.doc_id)
                    """.formatted(h), memberId, memberId);
            if (((Number) row.get("n_filings")).intValue() > 0) {
                Map<String, Object> withHorizon = new LinkedHashMap<>();
                withHorizon.put("horizon", h);
                withHorizon.putAll(row);
                rows.add(withHorizon);
            }
        }
        return rows;
    }

    /** Members with copyable buys that have outcomes, for the Outcomes page's picker. */
    public List<Map<String, Object>> outcomeMembers() {
        return jdbc.queryForList("""
                SELECT t.member_id, m.name, COUNT(DISTINCT t.doc_id) AS n_filings
                FROM trade_outcomes o
                JOIN trades t ON t.trade_id = o.trade_id
                JOIN members m ON m.member_id = t.member_id
                WHERE o.copyable = 1
                GROUP BY t.member_id, m.name
                ORDER BY m.name
                """);
    }

    /** Copyable buys with their outcomes, newest D0 first. */
    public List<Map<String, Object>> outcomeTrades(String memberId, int limit) {
        return jdbc.queryForList("""
                SELECT t.trade_id, t.symbol, t.asset_name, t.action, t.tx_date, t.disclosure_date,
                       COALESCE(m.name, f.filer_name) AS member_name, f.available_basis,
                       o.d0_date, o.ret_5, o.ret_20, o.ret_60, o.abn_ret_5, o.abn_ret_20, o.abn_ret_60, o.win_20,
                       o.tx_abn_ret, o.complete
                FROM trade_outcomes o
                JOIN trades t ON t.trade_id = o.trade_id
                LEFT JOIN members m ON m.member_id = t.member_id
                LEFT JOIN filings f ON f.doc_id = t.doc_id
                WHERE o.copyable = 1 AND (? = '' OR t.member_id = ?)
                ORDER BY o.d0_date DESC, t.trade_id DESC
                LIMIT ?
                """, memberId, memberId, limit);
    }

    /** Open-inflation aggregates and best entry delays (latest run), with member names for member groups. */
    public Map<String, Object> openInflation() {
        List<Map<String, Object>> stats = jdbc.queryForList("""
                SELECT s.group_type, s.group_key, s.k, s.n, s.n_trades, s.mean, s.median, s.share_pos, s.shrunk_mean,
                       s.computed_at, m.name
                FROM open_inflation_stats s
                LEFT JOIN members m ON s.group_type = 'member' AND m.member_id = s.group_key
                ORDER BY s.group_type, s.n DESC, s.group_key, s.k
                """);
        List<Map<String, Object>> delays = jdbc.queryForList("""
                SELECT d.group_type, d.group_key, d.n, d.n_trades, d.best_k, d.gain, d.computed_at, m.name
                FROM entry_delays d
                LEFT JOIN members m ON d.group_type = 'member' AND m.member_id = d.group_key
                ORDER BY d.group_type, d.n DESC, d.group_key
                """);
        return Map.of("stats", stats, "delays", delays);
    }

    /** One trade with its filing, member and outcome (empty if there's no such trade). */
    public Optional<Map<String, Object>> trade(long tradeId) {
        return jdbc.queryForList("""
                SELECT t.trade_id, t.doc_id, t.ticker, t.symbol, t.ticker_status, t.asset_name, t.asset_type,
                       t.action, t.owner, t.tx_date, t.disclosure_date, t.amount_min, t.amount_max,
                       t.filing_delay_days, t.committee_relevant, t.sector, t.industry, t.mcap_bucket, t.description,
                       COALESCE(m.name, f.filer_name) AS member_name, m.party, f.chamber, f.source_url, f.filing_date,
                       f.available_at, f.available_basis, f.doc_format, f.parse_method,
                       o.d0_date, o.d0_open, o.complete, o.copyable, o.tx_ret, o.tx_abn_ret,
                       o.ret_1, o.ret_5, o.ret_10, o.ret_20, o.ret_60,
                       o.abn_ret_1, o.abn_ret_5, o.abn_ret_10, o.abn_ret_20, o.abn_ret_60,
                       o.win_1, o.win_5, o.win_10, o.win_20, o.win_60,
                       o.open_infl_1, o.open_infl_2, o.open_infl_3, o.open_infl_5
                FROM trades t
                LEFT JOIN members m ON m.member_id = t.member_id
                LEFT JOIN filings f ON f.doc_id = t.doc_id
                LEFT JOIN trade_outcomes o ON o.trade_id = t.trade_id
                WHERE t.trade_id = ?
                """, tradeId).stream().findFirst();
    }

    /** Daily prices for the trade's symbol and SPY around the trade: from 10 days before the trade (or the
     *  disclosure) to about 65 trading days after D0. adj_open puts the open on the adj_close basis, so a
     *  chart indexed to the D0 adj_open matches the outcome returns. SPY's days are the calendar. */
    public List<Map<String, Object>> tradePrices(String symbol, String from, String to) {
        return jdbc.queryForList("""
                SELECT s.date,
                       p.adj_close AS price, p.open * p.adj_close / p.close AS price_adj_open,
                       s.adj_close AS spy, s.open * s.adj_close / s.close AS spy_adj_open
                FROM prices s
                LEFT JOIN prices p ON p.ticker = ? AND p.date = s.date
                WHERE s.ticker = 'SPY' AND s.date BETWEEN date(?, '-10 days') AND date(?, '+95 days')
                ORDER BY s.date
                """, symbol, from, to);
    }
}
