package com.tracker;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.Statement;
import java.util.List;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

/** Starts the app on a database created from the pre-migration schema and checks it gets upgraded. */
@SpringBootTest
class MigrationRunnerTest {

    @TempDir
    static Path tmp;

    @DynamicPropertySource
    static void oldDatabase(DynamicPropertyRegistry registry) throws Exception {
        Path db = tmp.resolve("old.db");
        String oldSchema = Files.readString(Path.of("../tests/fixtures/schema_v0.sql"));
        try (Connection c = DriverManager.getConnection("jdbc:sqlite:" + db); Statement s = c.createStatement()) {
            for (String statement : oldSchema.split(";")) {
                if (!statement.isBlank()) {
                    s.execute(statement);
                }
            }
            s.execute("INSERT INTO members (member_id, name, chamber) VALUES ('P1', 'Someone', 'house')");
            s.execute("INSERT INTO watchlist (member_id, added_at) VALUES ('P1', '2026-09-30T00:00:00Z')");
        }
        registry.add("tracker.db-path", db::toString);
        registry.add("tracker.open-browser", () -> "false");
    }

    @Autowired
    JdbcTemplate jdbc;

    @Test
    void upgradesOldDatabaseAndKeepsData() {
        List<String> columns = jdbc.queryForList("SELECT name FROM pragma_table_info('filings')", String.class);
        assertThat(columns).contains("filer_name", "state_district", "doc_format", "index_seen_at", "search_seen_at");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM sqlite_master WHERE name = 'source_state'", Integer.class))
                .isEqualTo(1);
        assertThat(jdbc.queryForList("SELECT name FROM pragma_table_info('trades')", String.class))
                .contains("line_no", "asset_name", "asset_code", "description", "symbol", "ticker_status", "is_etf");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_trades_doc_line'", Integer.class))
                .isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM sqlite_master WHERE name = 'pipeline_runs'", Integer.class))
                .isEqualTo(1);
        assertThat(jdbc.queryForList("SELECT name FROM pragma_table_info('trade_outcomes')", String.class))
                .contains("win_1", "win_60", "tx_ret", "tx_abn_ret", "computed_at", "copyable");
        assertThat(jdbc.queryForObject(
                "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('open_inflation_stats', 'entry_delays')", Integer.class))
                .isEqualTo(2);
        assertThat(jdbc.queryForList("SELECT name FROM pragma_table_info('member_scores')", String.class))
                .contains("horizon", "n_filings", "mean_ret", "mean_spy_ret", "median_abn_ret", "consistency");
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM sqlite_master WHERE name = 'member_horizon_stats'",
                Integer.class)).isEqualTo(1);
        assertThat(jdbc.queryForList("SELECT name FROM pragma_table_info('signal_factors')", String.class))
                .contains("factor", "level", "shrunk", "effect");
        assertThat(jdbc.queryForObject("PRAGMA user_version", Integer.class)).isEqualTo(9);
        assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM watchlist", Integer.class)).isEqualTo(1);
    }

    @Test
    void splitsStatementsSkippingComments() {
        String sql = "-- header\n\nALTER TABLE a ADD COLUMN b TEXT;\nCREATE TABLE x (\n  y TEXT\n);\n";
        assertThat(MigrationRunner.statements(sql))
                .containsExactly("ALTER TABLE a ADD COLUMN b TEXT;", "CREATE TABLE x (\n  y TEXT\n);");
    }
}
