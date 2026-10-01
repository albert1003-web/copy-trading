package com.tracker.repo;

import java.util.List;
import java.util.Map;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/** Read-only member leaderboard (latest member_scores snapshot). */
@Repository
public class ScoreRepository {

    private final JdbcTemplate jdbc;

    public ScoreRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public List<Map<String, Object>> leaderboard() {
        return jdbc.queryForList("""
                SELECT s.member_id, s.as_of, s.n_trades, s.mean_abn_ret, s.hit_rate, s.shrunk_score, s.rank,
                       m.name, m.chamber, m.party
                FROM member_scores s JOIN members m ON m.member_id = s.member_id
                WHERE s.as_of = (SELECT MAX(as_of) FROM member_scores)
                ORDER BY s.rank
                """);
    }
}
