package com.tracker;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.containsString;
import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.nullValue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.forwardedUrl;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.nio.file.Path;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;

/** Runs the real app (schema.sql, SQL, controllers) against a throwaway SQLite file. */
@SpringBootTest
@AutoConfigureMockMvc
class ApiTest {

    @TempDir
    static Path tmp;

    @DynamicPropertySource
    static void testDatabase(DynamicPropertyRegistry registry) {
        registry.add("tracker.db-path", () -> tmp.resolve("test.db").toString());
        registry.add("tracker.open-browser", () -> "false");
    }

    private static final String[] TABLES = {
            "alerts", "filing_alerts", "pipeline_runs", "trade_outcomes", "my_positions", "agent_runs", "member_scores", "watchlist",
            "trades", "filings", "prices", "exit_backtests", "members", "source_state", "price_coverage", "securities",
            "committee_memberships", "open_inflation_stats", "entry_delays"};

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @BeforeEach
    void seed() {
        for (String table : TABLES) {
            jdbc.update("DELETE FROM " + table);
        }
        jdbc.update("""
                INSERT INTO members (member_id, name, chamber, party, state, committees, active) VALUES
                  ('P000197', 'Nancy Pelosi', 'house', 'D', 'CA', '[]', 1),
                  ('T000278', 'Tommy Tuberville', 'senate', 'R', 'AL', '[]', 1),
                  ('X000001', 'Former Member', 'house', 'R', 'TX', '[]', 0)
                """);
        jdbc.update("""
                INSERT INTO filings (doc_id, member_id, chamber, filing_date, source_url, first_seen_at, parse_status) VALUES
                  ('H1', 'P000197', 'house',  '2026-09-28', 'https://example.com/h1.pdf', '2026-09-28T14:05:00Z', 'parsed'),
                  ('S1', 'T000278', 'senate', '2026-09-20', 'https://example.com/s1',     '2026-09-20T15:00:00Z', 'needs_review')
                """);
        jdbc.update("""
                INSERT INTO trades (trade_id, doc_id, member_id, ticker, asset_type, action, owner, tx_date,
                                    disclosure_date, amount_min, amount_max, filing_delay_days) VALUES
                  (1, 'H1', 'P000197', 'NVDA', 'stock', 'BUY',  'spouse', '2026-09-10', '2026-09-28', 1000001, 5000000, 18),
                  (2, 'H1', 'P000197', 'AAPL', 'stock', 'SELL', 'spouse', '2026-09-11', '2026-09-28', 15001, 50000, 17),
                  (3, 'S1', 'T000278', 'LMT',  'stock', 'BUY',  'self',   '2026-09-01', '2026-09-20', 1001, 15000, 19)
                """);
        jdbc.update("""
                INSERT INTO alerts (alert_id, trade_id, sent_at, score, suggested_entry, suggested_exit) VALUES
                  (1, 1, '2026-09-28T14:10:00Z', 0.82, 'D0 open', '20d hold'),
                  (2, 1, '2026-09-28T15:00:00Z', 0.60, NULL, NULL)
                """);
        jdbc.update("""
                INSERT INTO member_scores (member_id, as_of, n_trades, mean_abn_ret, hit_rate, shrunk_score, rank) VALUES
                  ('P000197', '2026-09-30', 42, 0.031, 0.62, 0.018, 1),
                  ('T000278', '2026-09-30', 30, 0.010, 0.55, 0.006, 2),
                  ('T000278', '2026-09-01', 25, 0.020, 0.60, 0.012, 1)
                """);
        jdbc.update("""
                INSERT INTO agent_runs (run_id, agent, started_at, output, approved) VALUES
                  (1, 'strategy_analyst', '2026-09-29T20:00:00Z', 'Add Tuberville.', NULL),
                  (2, 'daily_digest',     '2026-09-30T21:00:00Z', 'Quiet day.',      NULL)
                """);
    }

    private static MockHttpServletRequestBuilder postJson(String url, String json) {
        return post(url).contentType(MediaType.APPLICATION_JSON).content(json);
    }

    @Nested
    class SystemApi {
        @Test
        void health() throws Exception {
            mvc.perform(get("/api/health")).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("ok"));
        }

        @Test
        void summaryCountsEverything() throws Exception {
            mvc.perform(get("/api/summary"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.filings").value(2))
                    .andExpect(jsonPath("$.needs_review").value(1))
                    .andExpect(jsonPath("$.trades").value(3))
                    .andExpect(jsonPath("$.alerts").value(2))
                    .andExpect(jsonPath("$.watchlist").value(0))
                    .andExpect(jsonPath("$.open_positions").value(0))
                    .andExpect(jsonPath("$.last_filing_seen_at").value("2026-09-28T14:05:00Z"));
        }

        @Test
        void summaryOnEmptyDatabase() throws Exception {
            for (String table : TABLES) {
                jdbc.update("DELETE FROM " + table);
            }
            mvc.perform(get("/api/summary"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.trades").value(0))
                    .andExpect(jsonPath("$.last_filing_seen_at").value(nullValue()));
        }

        @Test
        void recentFilingsNewestFirstWithMemberName() throws Exception {
            mvc.perform(get("/api/filings/recent"))
                    .andExpect(jsonPath("$", hasSize(2)))
                    .andExpect(jsonPath("$[0].doc_id").value("H1"))
                    .andExpect(jsonPath("$[0].member_name").value("Nancy Pelosi"))
                    .andExpect(jsonPath("$[1].parse_status").value("needs_review"));
        }

        @Test
        void recentFilingsFallBackToFilerNameBeforeMemberIsResolved() throws Exception {
            jdbc.update("""
                    INSERT INTO filings (doc_id, member_id, chamber, filing_date, first_seen_at, parse_status,
                                         filer_name, state_district, doc_format)
                    VALUES ('9116342', NULL, 'house', '2026-09-30', '2026-09-30T13:00:00Z', 'needs_review',
                            'Hon. Harold Dallas Rogers', 'KY05', 'scanned')
                    """);
            mvc.perform(get("/api/filings/recent"))
                    .andExpect(jsonPath("$[0].doc_id").value("9116342"))
                    .andExpect(jsonPath("$[0].member_name").value("Hon. Harold Dallas Rogers"))
                    .andExpect(jsonPath("$[0].state_district").value("KY05"))
                    .andExpect(jsonPath("$[0].doc_format").value("scanned"));
        }

        @Test
        void recentFilingsSeenInTheSameRunAreOrderedByFilingDate() throws Exception {
            jdbc.update("""
                    INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at) VALUES
                      ('A', 'house', '2026-04-06', '2026-10-01T08:00:00Z'),
                      ('B', 'house', '2026-09-27', '2026-10-01T08:00:00Z'),
                      ('C', 'house', '2026-07-13', '2026-10-01T08:00:00Z')
                    """);
            mvc.perform(get("/api/filings/recent?limit=3"))
                    .andExpect(jsonPath("$[0].doc_id").value("B"))
                    .andExpect(jsonPath("$[1].doc_id").value("C"))
                    .andExpect(jsonPath("$[2].doc_id").value("A"));
        }

        @Test
        void pipelineHealthOnAnEmptyDatabase() throws Exception {
            mvc.perform(get("/api/pipeline/health"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.last_run").value(nullValue()))
                    .andExpect(jsonPath("$.last_ok_at").value(nullValue()))
                    .andExpect(jsonPath("$.failure_streak").value(0))
                    .andExpect(jsonPath("$.runs_24h").value(0));
            mvc.perform(get("/api/pipeline/runs")).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(0)));
        }

        @Test
        void pipelineHealthReportsTheLatestRunAndFailureStreak() throws Exception {
            String recent = java.time.Instant.now().minusSeconds(600).truncatedTo(java.time.temporal.ChronoUnit.SECONDS).toString();
            jdbc.update("""
                    INSERT INTO pipeline_runs (run_id, started_at, finished_at, status, stages, warnings) VALUES
                      (1, '2026-09-01T10:00:00Z', '2026-09-01T10:01:00Z', 'failed', '{}', '[]'),
                      (2, '2026-09-01T10:30:00Z', '2026-09-01T10:31:00Z', 'ok', '{}', '[]'),
                      (3, '2026-09-01T11:00:00Z', '2026-09-01T11:01:00Z', 'failed', '{}', '[]'),
                      (4, ?, ?, 'failed', '{"ingest_senate": {"ok": false}}', '["w"]')
                    """, recent, recent);
            mvc.perform(get("/api/pipeline/health"))
                    .andExpect(jsonPath("$.last_run.run_id").value(4))
                    .andExpect(jsonPath("$.last_run.status").value("failed"))
                    .andExpect(jsonPath("$.last_run.stages").value("{\"ingest_senate\": {\"ok\": false}}"))
                    .andExpect(jsonPath("$.last_ok_at").value("2026-09-01T10:30:00Z"))
                    .andExpect(jsonPath("$.failure_streak").value(2))
                    .andExpect(jsonPath("$.runs_24h").value(1));
            mvc.perform(get("/api/pipeline/runs?limit=3"))
                    .andExpect(jsonPath("$", hasSize(3)))
                    .andExpect(jsonPath("$[0].run_id").value(4))
                    .andExpect(jsonPath("$[0].warnings").value("[\"w\"]"))
                    .andExpect(jsonPath("$[2].run_id").value(2));
        }

        @Test
        void pipelineHistoryOnAnEmptyDatabase() throws Exception {
            for (String table : TABLES) {
                jdbc.update("DELETE FROM " + table);
            }
            mvc.perform(get("/api/pipeline/history"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.price_coverage.ok").value(0))
                    .andExpect(jsonPath("$.price_coverage.missing").value(0))
                    .andExpect(jsonPath("$.trades_with_symbol").value(0))
                    .andExpect(jsonPath("$.trades_priced").value(0))
                    .andExpect(jsonPath("$.review_queue", hasSize(0)))
                    .andExpect(jsonPath("$.last_nightly").value(nullValue()));
        }

        @Test
        void pipelineHistoryReportsCoverageAndTheReviewQueue() throws Exception {
            jdbc.update("UPDATE trades SET symbol = ticker, ticker_status = 'listed'");
            jdbc.update("UPDATE filings SET filing_year = 2026, doc_format = 'electronic'");
            jdbc.update("""
                    INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at, parse_status, filing_year, doc_format)
                    VALUES ('OLD', 'senate', '2020-05-01', '2026-10-01T00:00:00Z', 'needs_review', 2020, 'scanned')
                    """);
            jdbc.update("""
                    INSERT INTO price_coverage (symbol, n_rows, status, checked_at) VALUES
                      ('NVDA', 10, 'ok', '2026-10-01T22:00:00Z'), ('AAPL', 10, 'ok', '2026-10-01T22:00:00Z'),
                      ('LMT', 0, 'missing', '2026-10-01T22:00:00Z')
                    """);
            jdbc.update("INSERT INTO source_state (source, checked_at) VALUES ('pipeline.nightly', '2026-10-01')");
            mvc.perform(get("/api/pipeline/history"))
                    .andExpect(jsonPath("$.price_coverage.ok").value(2))
                    .andExpect(jsonPath("$.price_coverage.partial").value(0))
                    .andExpect(jsonPath("$.price_coverage.missing").value(1))
                    .andExpect(jsonPath("$.trades_with_symbol").value(3))
                    .andExpect(jsonPath("$.trades_priced").value(2))
                    .andExpect(jsonPath("$.review_queue", hasSize(3)))
                    .andExpect(jsonPath("$.review_queue[0].year").value(2026))
                    .andExpect(jsonPath("$.review_queue[2].year").value(2020))
                    .andExpect(jsonPath("$.review_queue[2].scanned").value(1))
                    .andExpect(jsonPath("$.last_nightly").value("2026-10-01"));
        }

        @Test
        void schemaIsAtLatestMigration() {
            assertThat(jdbc.queryForObject("PRAGMA user_version", Integer.class)).isEqualTo(7);
        }

        @Test
        void recentAlertsJoinTradeAndMember() throws Exception {
            mvc.perform(get("/api/alerts/recent?limit=1"))
                    .andExpect(jsonPath("$", hasSize(1)))
                    .andExpect(jsonPath("$[0].alert_id").value(2))
                    .andExpect(jsonPath("$[0].ticker").value("NVDA"))
                    .andExpect(jsonPath("$[0].member_name").value("Nancy Pelosi"));
        }
    }

    @Nested
    class Trades {
        @Test
        void allTradesNewestDisclosureFirstWithMaxAlertScore() throws Exception {
            mvc.perform(get("/api/trades"))
                    .andExpect(jsonPath("$", hasSize(3)))
                    .andExpect(jsonPath("$[0].ticker").value("AAPL"))
                    .andExpect(jsonPath("$[1].ticker").value("NVDA"))
                    .andExpect(jsonPath("$[1].score").value(0.82))
                    .andExpect(jsonPath("$[1].source_url").value("https://example.com/h1.pdf"))
                    .andExpect(jsonPath("$[2].ticker").value("LMT"))
                    .andExpect(jsonPath("$[2].score").value(nullValue()));
        }

        @Test
        void tradesCarrySectorCommitteeAndAvailabilityBasis() throws Exception {
            jdbc.update("UPDATE trades SET sector = 'Industrials', mcap_bucket = 'large', committee_relevant = 1 WHERE trade_id = 3");
            jdbc.update("UPDATE filings SET available_basis = 'filed' WHERE doc_id = 'S1'");
            mvc.perform(get("/api/trades?ticker=LMT"))
                    .andExpect(jsonPath("$[0].sector").value("Industrials"))
                    .andExpect(jsonPath("$[0].mcap_bucket").value("large"))
                    .andExpect(jsonPath("$[0].committee_relevant").value(1))
                    .andExpect(jsonPath("$[0].available_basis").value("filed"));
            mvc.perform(get("/api/trades?ticker=NVDA")).andExpect(jsonPath("$[0].available_basis").value("seen"));
        }

        @Test
        void filterByTickerIsCaseInsensitive() throws Exception {
            mvc.perform(get("/api/trades?ticker=nvda"))
                    .andExpect(jsonPath("$", hasSize(1)))
                    .andExpect(jsonPath("$[0].trade_id").value(1));
        }

        @Test
        void filterByMemberNameSubstring() throws Exception {
            mvc.perform(get("/api/trades?member=tuber"))
                    .andExpect(jsonPath("$", hasSize(1)))
                    .andExpect(jsonPath("$[0].ticker").value("LMT"));
        }

        @Test
        void filterByAction() throws Exception {
            mvc.perform(get("/api/trades?action=SELL"))
                    .andExpect(jsonPath("$", hasSize(1)))
                    .andExpect(jsonPath("$[0].ticker").value("AAPL"));
        }

        @Test
        void filtersCombineAndLimitApplies() throws Exception {
            mvc.perform(get("/api/trades?member=pelosi&action=BUY")).andExpect(jsonPath("$", hasSize(1)));
            mvc.perform(get("/api/trades?limit=2")).andExpect(jsonPath("$", hasSize(2)));
        }

        @Test
        void tickerFilterMatchesTheValidatedSymbolToo() throws Exception {
            jdbc.update("UPDATE trades SET ticker = 'SQ', symbol = 'XYZ', ticker_status = 'renamed' WHERE trade_id = 2");
            mvc.perform(get("/api/trades?ticker=xyz"))
                    .andExpect(jsonPath("$", hasSize(1)))
                    .andExpect(jsonPath("$[0].ticker").value("SQ"))
                    .andExpect(jsonPath("$[0].symbol").value("XYZ"))
                    .andExpect(jsonPath("$[0].ticker_status").value("renamed"));
            mvc.perform(get("/api/trades?ticker=sq")).andExpect(jsonPath("$", hasSize(1)));
        }

        @Test
        void unresolvedFilerNameIsShownAndSearchable() throws Exception {
            jdbc.update("""
                    INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at, parse_status, filer_name)
                    VALUES ('H9', 'house', '2026-09-29', '2026-09-29T14:00:00Z', 'parsed', 'Hon. Max Miller')
                    """);
            jdbc.update("""
                    INSERT INTO trades (trade_id, doc_id, line_no, asset_name, asset_type, action, owner, tx_date,
                                        disclosure_date, amount_min, amount_max)
                    VALUES (9, 'H9', 1, 'Mercato Partners Traverse IV QP, LP', 'other', 'BUY', 'self', '2026-09-04',
                            '2026-09-29', 1001, 15000)
                    """);
            mvc.perform(get("/api/trades?member=miller"))
                    .andExpect(jsonPath("$", hasSize(1)))
                    .andExpect(jsonPath("$[0].member_name").value("Hon. Max Miller"))
                    .andExpect(jsonPath("$[0].chamber").value("house"))
                    .andExpect(jsonPath("$[0].ticker").value(nullValue()))
                    .andExpect(jsonPath("$[0].asset_name").value("Mercato Partners Traverse IV QP, LP"));
        }

        @Test
        void leaderboardUsesLatestSnapshotOnly() throws Exception {
            mvc.perform(get("/api/leaderboard"))
                    .andExpect(jsonPath("$", hasSize(2)))
                    .andExpect(jsonPath("$[0].name").value("Nancy Pelosi"))
                    .andExpect(jsonPath("$[1].as_of").value("2026-09-30"));
        }
    }

    @Nested
    class Watchlist {
        @Test
        void membersListsActiveOnly() throws Exception {
            mvc.perform(get("/api/members"))
                    .andExpect(jsonPath("$", hasSize(2)))
                    .andExpect(jsonPath("$[0].name").value("Nancy Pelosi"))
                    .andExpect(jsonPath("$[0].watched").value(0));
        }

        @Test
        void addRemoveAndReAdd() throws Exception {
            mvc.perform(postJson("/api/watchlist", "{\"memberId\":\"T000278\",\"reason\":\"defense\"}"))
                    .andExpect(status().isNoContent());
            mvc.perform(get("/api/watchlist"))
                    .andExpect(jsonPath("$", hasSize(1)))
                    .andExpect(jsonPath("$[0].name").value("Tommy Tuberville"))
                    .andExpect(jsonPath("$[0].reason").value("defense"));
            mvc.perform(get("/api/members")).andExpect(jsonPath("$[1].watched").value(1));

            mvc.perform(delete("/api/watchlist/T000278")).andExpect(status().isNoContent());
            mvc.perform(get("/api/watchlist")).andExpect(jsonPath("$", hasSize(0)));

            mvc.perform(postJson("/api/watchlist", "{\"memberId\":\"T000278\",\"reason\":\"again\"}"))
                    .andExpect(status().isNoContent());
            mvc.perform(get("/api/watchlist")).andExpect(jsonPath("$[0].reason").value("again"));
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM watchlist", Integer.class)).isEqualTo(1);
        }

        @Test
        void rejectsMissingMemberId() throws Exception {
            mvc.perform(postJson("/api/watchlist", "{}")).andExpect(status().isBadRequest());
            mvc.perform(postJson("/api/watchlist", "{\"memberId\":\"  \"}")).andExpect(status().isBadRequest());
        }

        @Test
        void rejectsUnknownMember() throws Exception {
            // Foreign keys are on, so a typo can't create an orphan watchlist row.
            mvc.perform(postJson("/api/watchlist", "{\"memberId\":\"NOPE\"}")).andExpect(status().isBadRequest());
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM watchlist", Integer.class)).isZero();
        }
    }

    @Nested
    class Positions {
        @Test
        void openThenClose() throws Exception {
            mvc.perform(postJson("/api/positions",
                            "{\"ticker\":\" nvda \",\"buyDate\":\"2026-09-29\",\"buyPrice\":180.5,\"shares\":10,\"tradeId\":1,\"exitRule\":\"20d hold\"}"))
                    .andExpect(status().isNoContent());
            mvc.perform(get("/api/positions"))
                    .andExpect(jsonPath("$", hasSize(1)))
                    .andExpect(jsonPath("$[0].ticker").value("NVDA"))
                    .andExpect(jsonPath("$[0].status").value("open"))
                    .andExpect(jsonPath("$[0].trade_id").value(1));
            mvc.perform(get("/api/summary")).andExpect(jsonPath("$.open_positions").value(1));

            long id = jdbc.queryForObject("SELECT position_id FROM my_positions", Long.class);
            mvc.perform(postJson("/api/positions/" + id + "/close", "{\"sellDate\":\"2026-10-01\",\"sellPrice\":190}"))
                    .andExpect(status().isNoContent());
            mvc.perform(get("/api/positions"))
                    .andExpect(jsonPath("$[0].status").value("closed"))
                    .andExpect(jsonPath("$[0].sell_price").value(190.0));

            // Closing twice is a 404, not a silent overwrite.
            mvc.perform(postJson("/api/positions/" + id + "/close", "{\"sellDate\":\"2026-10-02\",\"sellPrice\":200}"))
                    .andExpect(status().isNotFound());
        }

        @Test
        void openPositionsListFirst() throws Exception {
            jdbc.update("""
                    INSERT INTO my_positions (position_id, ticker, buy_date, buy_price, shares, status, sell_date, sell_price)
                    VALUES (1, 'OLD', '2026-01-01', 10, 1, 'closed', '2026-02-01', 12),
                           (2, 'NEW', '2025-12-01', 10, 1, 'open', NULL, NULL)
                    """);
            mvc.perform(get("/api/positions")).andExpect(jsonPath("$[0].ticker").value("NEW"));
        }

        @Test
        void rejectsInvalidInput() throws Exception {
            mvc.perform(postJson("/api/positions", "{\"buyDate\":\"2026-09-29\",\"buyPrice\":1,\"shares\":1}"))
                    .andExpect(status().isBadRequest());
            mvc.perform(postJson("/api/positions", "{\"ticker\":\"X\",\"buyDate\":\"2026-09-29\",\"buyPrice\":0,\"shares\":1}"))
                    .andExpect(status().isBadRequest());
            mvc.perform(postJson("/api/positions", "{\"ticker\":\"X\",\"buyDate\":\"2026-09-29\",\"buyPrice\":1,\"shares\":-2}"))
                    .andExpect(status().isBadRequest());
            mvc.perform(postJson("/api/positions/99/close", "{\"sellDate\":\"2026-10-01\"}"))
                    .andExpect(status().isBadRequest());
            mvc.perform(postJson("/api/positions/99/close", "{\"sellDate\":\"2026-10-01\",\"sellPrice\":5}"))
                    .andExpect(status().isNotFound());
            mvc.perform(postJson("/api/positions", "{\"ticker\":\"X\",\"buyDate\":\"2026-09-29\",\"buyPrice\":1,\"shares\":1,\"tradeId\":999}"))
                    .andExpect(status().isBadRequest());
            assertThat(jdbc.queryForObject("SELECT COUNT(*) FROM my_positions", Integer.class)).isZero();
        }
    }

    @Nested
    class AgentRuns {
        @Test
        void listsNewestFirstAsPending() throws Exception {
            mvc.perform(get("/api/agent-runs"))
                    .andExpect(jsonPath("$", hasSize(2)))
                    .andExpect(jsonPath("$[0].agent").value("daily_digest"))
                    .andExpect(jsonPath("$[0].approved").value(nullValue()));
        }

        @Test
        void approveAndReject() throws Exception {
            mvc.perform(postJson("/api/agent-runs/1/decision", "{\"approved\":true}")).andExpect(status().isNoContent());
            mvc.perform(postJson("/api/agent-runs/2/decision", "{\"approved\":false}")).andExpect(status().isNoContent());
            assertThat(jdbc.queryForObject("SELECT approved FROM agent_runs WHERE run_id = 1", Integer.class)).isEqualTo(1);
            assertThat(jdbc.queryForObject("SELECT approved FROM agent_runs WHERE run_id = 2", Integer.class)).isZero();
        }

        @Test
        void unknownRunIs404() throws Exception {
            mvc.perform(postJson("/api/agent-runs/99/decision", "{\"approved\":true}")).andExpect(status().isNotFound());
        }
    }

    @Nested
    class Frontend {
        @Test
        void servesIndexAtRoot() throws Exception {
            // Spring Boot's welcome-page mapping forwards "/" to index.html (MockMvc doesn't follow forwards).
            mvc.perform(get("/")).andExpect(status().isOk()).andExpect(forwardedUrl("index.html"));
            mvc.perform(get("/index.html")).andExpect(status().isOk()).andExpect(content().string(containsString("<div id=\"root\">")));
        }

        @Test
        void clientRoutesFallBackToIndex() throws Exception {
            mvc.perform(get("/trades")).andExpect(status().isOk()).andExpect(content().string(containsString("<div id=\"root\">")));
            // index.html is always revalidated, so a reinstalled app never shows the previous build's UI
            mvc.perform(get("/index.html")).andExpect(header().string("Cache-Control", "no-cache"));
            mvc.perform(get("/trades")).andExpect(header().string("Cache-Control", "no-cache"));
            mvc.perform(get("/positions/123")).andExpect(status().isOk()).andExpect(content().string(containsString("<div id=\"root\">")));
        }

        @Test
        void unknownApiPathsAre404NotIndex() throws Exception {
            mvc.perform(get("/api/nope")).andExpect(status().isNotFound());
        }
    }
}
