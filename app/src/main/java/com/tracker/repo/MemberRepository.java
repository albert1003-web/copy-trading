package com.tracker.repo;

import java.time.Instant;
import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Members (read-only) and the watchlist (user-owned). */
@Repository
public class MemberRepository {

    private final JdbcTemplate jdbc;

    public MemberRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public List<Map<String, Object>> members() {
        return jdbc.queryForList("""
                SELECT m.member_id, m.name, m.chamber, m.party, m.state,
                       COALESCE(w.active, 0) AS watched
                FROM members m LEFT JOIN watchlist w ON w.member_id = m.member_id
                WHERE m.active = 1
                ORDER BY m.name
                """);
    }

    public List<Map<String, Object>> watchlist() {
        return jdbc.queryForList("""
                SELECT w.member_id, w.added_at, w.reason, m.name, m.chamber, m.party, m.state
                FROM watchlist w JOIN members m ON m.member_id = w.member_id
                WHERE w.active = 1
                ORDER BY m.name
                """);
    }

    public void watch(String memberId, String reason) {
        jdbc.update("""
                INSERT INTO watchlist (member_id, added_at, reason, active) VALUES (?, ?, ?, 1)
                ON CONFLICT(member_id) DO UPDATE SET active = 1, added_at = excluded.added_at, reason = excluded.reason
                """, memberId, Instant.now().toString(), reason);
    }

    public void unwatch(String memberId) {
        jdbc.update("UPDATE watchlist SET active = 0 WHERE member_id = ?", memberId);
    }
}
