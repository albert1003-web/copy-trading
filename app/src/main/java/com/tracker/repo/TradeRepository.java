package com.tracker.repo;

import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Read-only access to filings, trades and alerts (written by the Python pipelines). */
@Repository
public class TradeRepository {

    private final JdbcTemplate jdbc;

    public TradeRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Map<String, Object> summary() {
        return jdbc.queryForMap("""
                SELECT
                  (SELECT COUNT(*) FROM filings)                                       AS filings,
                  (SELECT COUNT(*) FROM filings WHERE parse_status = 'needs_review')   AS needs_review,
                  (SELECT COUNT(*) FROM trades)                                        AS trades,
                  (SELECT COUNT(*) FROM alerts)                                        AS alerts,
                  (SELECT COUNT(*) FROM watchlist WHERE active = 1)                    AS watchlist,
                  (SELECT COUNT(*) FROM my_positions WHERE status = 'open')            AS open_positions,
                  (SELECT MAX(first_seen_at) FROM filings)                             AS last_filing_seen_at
                """);
    }

    public List<Map<String, Object>> recentFilings(int limit) {
        return jdbc.queryForList("""
                SELECT f.doc_id, f.chamber, f.filing_date, f.source_url, f.first_seen_at, f.parse_status,
                       f.state_district, f.doc_format,
                       COALESCE(m.name, f.filer_name) AS member_name
                FROM filings f LEFT JOIN members m ON m.member_id = f.member_id
                ORDER BY f.first_seen_at DESC, f.filing_date DESC, f.doc_id DESC
                LIMIT ?
                """, limit);
    }

    public List<Map<String, Object>> recentAlerts(int limit) {
        return jdbc.queryForList("""
                SELECT a.alert_id, a.sent_at, a.score, a.suggested_entry, a.suggested_exit,
                       t.ticker, t.action, t.amount_min, t.amount_max, m.name AS member_name
                FROM alerts a
                JOIN trades t ON t.trade_id = a.trade_id
                LEFT JOIN members m ON m.member_id = t.member_id
                ORDER BY a.sent_at DESC
                LIMIT ?
                """, limit);
    }

    /** Trades, newest disclosure first. Blank filters are ignored. Until filers are resolved to members (M1.4),
     *  member_name falls back to the filer name as the source lists it. */
    public List<Map<String, Object>> trades(String member, String ticker, String action, int limit) {
        return jdbc.queryForList("""
                SELECT t.trade_id, t.ticker, t.symbol, t.ticker_status, t.asset_name, t.asset_type, t.action, t.owner, t.tx_date, t.disclosure_date,
                       t.amount_min, t.amount_max, t.filing_delay_days, t.committee_relevant, t.confidence,
                       t.sector, t.industry, t.mcap_bucket,
                       COALESCE(m.name, f.filer_name) AS member_name, m.party, f.chamber, f.source_url,
                       f.available_basis, a.score
                FROM trades t
                LEFT JOIN members m ON m.member_id = t.member_id
                LEFT JOIN filings f ON f.doc_id = t.doc_id
                LEFT JOIN (SELECT trade_id, MAX(score) AS score FROM alerts GROUP BY trade_id) a
                       ON a.trade_id = t.trade_id
                WHERE (? = '' OR COALESCE(m.name, f.filer_name) LIKE '%' || ? || '%')
                  AND (? = '' OR UPPER(?) IN (t.ticker, t.symbol))
                  AND (? = '' OR t.action = ?)
                ORDER BY t.disclosure_date DESC, t.trade_id DESC
                LIMIT ?
                """, member, member, ticker, ticker, action, action, limit);
    }
}
