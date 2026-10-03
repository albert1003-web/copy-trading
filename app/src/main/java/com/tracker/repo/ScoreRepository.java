package com.tracker.repo;

import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Read-only member leaderboard (latest member_scores snapshot; unranked members last). */
@Repository
public class ScoreRepository {

    private final JdbcTemplate jdbc;

    public ScoreRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public List<Map<String, Object>> leaderboard() {
        return jdbc.queryForList("""
                SELECT s.member_id, s.as_of, s.n_trades, s.mean_abn_ret, s.hit_rate, s.shrunk_score, s.rank,
                       s.horizon, s.n_filings, s.mean_ret, s.mean_spy_ret, s.median_abn_ret, s.consistency,
                       m.name, m.chamber, m.party
                FROM member_scores s JOIN members m ON m.member_id = s.member_id
                WHERE s.as_of = (SELECT MAX(as_of) FROM member_scores)
                ORDER BY s.rank IS NULL, s.rank, s.n_filings DESC
                """);
    }
}
