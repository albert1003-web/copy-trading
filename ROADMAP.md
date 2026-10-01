# Roadmap

Derived from `Congressional Trade Tracker – Design Doc.pdf` (Draft v1, Sep 23, 2026). Each milestone has a concrete exit criterion. Check items off as they land.

Feature key: **F1** alerts · **F2** S&P benchmark · **F3** open-price inflation · **F4** exit timing · **F5** trade scorecard · **F6** member leaderboard · **F7** agent layer

---

## Milestone 0: Project foundation

Goal: an empty but runnable project skeleton.

- [x] `pyproject.toml` (Python 3.12), dev dependencies (pytest, ruff)
- [x] Packages `common/`, `db/`, `ingest/` (others are added with their milestones)
- [x] `.gitignore` (`.env`, `.venv/`, `*.db`, build output); data lives in `~/TradeTracker/`, outside the repo
- [x] `.env.example` (contact for the User-Agent, Gmail address, app password, alert recipient, Anthropic API key)
- [x] `db/schema.sql` for all 11 tables: `members`, `filings`, `trades`, `prices`, `trade_outcomes`, `exit_backtests`, `member_scores`, `watchlist`, `alerts`, `my_positions`, `agent_runs`
- [x] Python DB helper (`db.connect()`): applies `db/schema.sql` + migrations, honors `TRACKER_DB_PATH` (default `~/TradeTracker/tracker.db`, the same file the app uses)
- [x] Migrations (`db/migrations/`, `PRAGMA user_version`), applied by both Python and the app (`MigrationRunner`)
- [x] Logging setup (console + `~/TradeTracker/logs/pipeline.log`)
- [x] Fill in the Python **Commands** section of `CLAUDE.md`

**Done when:** `pytest` runs green, and `python -m db.init` creates the database. ✅

---

## Milestone 0.5: Desktop app shell (Spring Boot + React) ✅

Goal: a local macOS app the user double-clicks to see the data. It replaces the Streamlit dashboard from the design doc.

- [x] Spring Boot 3.3 (Java 17) app in `app/`: REST API plus React static files in one jar
- [x] Binds to `127.0.0.1:8787` only; single-instance check; Quit button (`POST /api/shutdown`)
- [x] Standalone app window (Chrome `--app` mode); closing the window quits the app
- [x] Shared SQLite DB via `JdbcTemplate`, with schema applied from `db/schema.sql` on startup (WAL + busy timeout)
- [x] React + Vite + TypeScript frontend, built into the jar by `frontend-maven-plugin`
- [x] Pages with empty states: Dashboard, Trades (filters), Watchlist (add/remove), Leaderboard, Positions (log buy / close), Agents (approve/reject)
- [x] `package-mac.sh` builds `Trade Tracker.app` with a bundled Java runtime (`--install` puts it in `~/Applications` with a Desktop shortcut)
- [x] App icon; background server hidden from the Dock (`LSUIElement`)
- [x] Backend tests: `ApiTest` (`@SpringBootTest` + MockMvc against a temp SQLite file), covering every endpoint, filters, validation, FK errors → 400, and SPA routing
- [x] Frontend smoke tests: Vitest + Testing Library; every page renders on an empty DB, plus key interactions (filters, watchlist add, log buy, approve, Quit, error banner)
- [x] Both suites run in `mvn test` / `mvn package` (`-DskipTests` skips both)

**Done when:** double-clicking `Trade Tracker.app` opens the UI in its own window and shows data from the shared DB. *(Verified against a scratch DB.)*

---

## Phase 1: Alerts (F1)

### Milestone 1.1: House ingestion ✅
- [x] Poll the Clerk's yearly filing index (ZIP/XML) for PTRs (`FilingType = P`), with conditional GET (ETag → 304)
- [x] Also poll the live search page every run, recording when each source first lists a filing (`index_seen_at`, `search_seen_at`)
- [x] Download new PTR PDFs to `~/TradeTracker/raw/house/<year>/<doc_id>.pdf` (atomic writes, `%PDF` check, files already on disk are reused)
- [x] Insert into `filings` with `first_seen_at`, filer name/district, `doc_format` (electronic/scanned), and `parse_status` (`pending`, or `needs_review` for scans)
- [x] Polite client: User-Agent, retries with backoff, 1 s between downloads, stop after 5 consecutive failures
- [x] App: recent filings show filer, district, filing date, and a scanned tag
- [ ] Decide whether the index lags: run `python -m ingest.house --lag-report` after a week of scheduled polling (M1.6), then drop the slower source or keep both

**Done when:** a run picks up all new House PTRs with no duplicates on re-run. *(Verified live: 403 PTRs for 2026; a re-run returns 304 and adds nothing.)*

### Milestone 1.2: Senate ingestion ✅
- [x] Session that accepts the eFD terms agreement (CSRF token), and accepts it again when the session expires
- [x] Search PTRs (senators and former senators) by date received, paged, from a week before the last successful search; fetch each report's HTML
- [x] Cache HTML to `~/TradeTracker/raw/senate/<year>/<uuid>.html` (validated, atomic writes, reused if on disk), insert into `filings`
- [x] Paper (scanned) reports marked `doc_format = scanned`, `parse_status = needs_review`

**Done when:** a run picks up all new Senate PTRs with no duplicates on re-run. *(Verified live: 133 PTRs for 2026 (9 paper); a re-run adds and downloads nothing.)*

### Milestone 1.3: Parsing & normalization
- [ ] House electronic PTR parser (pdfplumber + regex; port the `congress_alerts.py` prototype)
- [ ] Senate HTML table parser (BeautifulSoup)
- [ ] Normalize action, owner, asset type, and amount range (min/max ints)
- [ ] Mark scanned/paper filings `needs_review`
- [ ] Fixtures: at least 5 House PDFs and 5 Senate HTML reports, with parser tests

**Done when:** all fixtures parse to the expected rows, and `trades` gets populated from live filings.

### Milestone 1.4: Enrichment (basic)
- [ ] Ticker validation against a symbol list, plus a renamed/delisted resolver
- [ ] `members` table seeded (name, chamber, party, state)
- [ ] `filing_delay_days` computed

**Done when:** every parsed trade has a validated ticker or a flagged reason why it doesn't.

### Milestone 1.5: Scoring & email alerts
- [ ] Initial watchlist (see open question 1), managed from the app's Watchlist page
- [ ] v1 rule-based score: watchlist member, purchase, amount range, filing delay, stock vs fund
- [ ] Gmail SMTP email: member, ticker, action, amount, trade/disclosure dates, delay, score, source link
- [ ] Record each sent alert in `alerts`, never sending the same one twice

**Done when:** an email arrives for a real watchlist filing.

### Milestone 1.6: Scheduling & reliability
- [ ] Local cron: poll every 30 min on weekdays
- [ ] Failure alert email after N consecutive failures
- [ ] Detection-latency report built from `first_seen_at`

**Phase 1 exit:** two weeks of alerts with **no missed filings** compared with a public tracker.

---

## Phase 2: History & prices

### Milestone 2.1: Historical backfill
- [ ] Backfill House and Senate PTRs from 2020 to the present
- [ ] Re-parse from the raw cache, and size the `needs_review` queue

### Milestone 2.2: Full enrichment
- [ ] Sector/industry and market-cap bucket
- [ ] Committee assignments and the `committee_relevant` flag

### Milestone 2.3: Price pipeline
- [ ] Nightly OHLCV for every traded ticker plus SPY (yfinance)
- [ ] Store adjusted close (returns) and raw open (entry)
- [ ] Gap report: tickers with missing or delisted price history

**Phase 2 exit:** full trade history with prices in the DB.

---

## Phase 3: Analytics & dashboard (F2, F3, F5, F6)

### Milestone 3.1: Trade outcomes (F2, F5)
- [ ] Compute D0, Return(h), and abnormal return(h) for h ∈ {1, 5, 10, 20, 60}
- [ ] Fill in horizons as they mature, and mark trades complete at 60 trading days
- [ ] Win/loss labels, plus the trade-date return (context only)

### Milestone 3.2: Open-price inflation (F3)
- [ ] Open inflation(k) for k ∈ {1, 2, 3, 5}
- [ ] Aggregate by member, market-cap bucket, and media attention
- [ ] Best entry delay per member and size bucket

### Milestone 3.3: Member leaderboard (F6)
- [ ] Mean/median abnormal return, hit rate, n trades, consistency
- [ ] Empirical-Bayes shrunk score and a minimum sample size
- [ ] Nightly `member_scores` snapshot

### Milestone 3.4: Analytics views in the app
- [ ] Leaderboard page live on real `member_scores` (page already built in M0.5)
- [ ] Outcomes page: abnormal returns by horizon per trade/member (charts)
- [ ] Open-inflation page: k = 1/2/3/5 by member and market-cap bucket
- [ ] Trade detail view: filing, outcome, price chart vs SPY

### Milestone 3.5: v2 scoring
- [ ] Replace the rule-based score with one driven by the leaderboard and outcomes
- [ ] Add suggested entry timing (from F3) to alert emails

**Phase 3 exit:** leaderboard and inflation results reviewed, with a go/no-go note written on whether a post-disclosure edge exists.

---

## Phase 4: Exit timing (F4)

### Milestone 4.1: Backtest engine
- [ ] Exit rules: fixed holds, stop-loss/take-profit, trailing stop, ATR stop, exit on member sale
- [ ] Costs, slippage, and the Roth T+1 settled-cash constraint

### Milestone 4.2: Walk-forward validation
- [ ] Train on a rolling 2 years, test on the next quarter, report out-of-sample results only
- [ ] Small parameter grid, with results in `exit_backtests`

### Milestone 4.3: Exits in alerts
- [ ] Recommended exit rule plus confidence in alert emails

**Phase 4 exit:** out-of-sample results beat a simple fixed-hold baseline, or a written conclusion that they don't.

---

## Phase 5: Agents (F7)

### Milestone 5.1: Agent foundation
- [ ] Read-only SQL tools (`agents/tools.py`), web search, and `agent_runs` logging
- [ ] Proposal format (structured output); approvals are made in the app's Agents page (built in M0.5)
- [ ] Apply approved proposals (e.g. watchlist changes) in a pipeline job that reads `agent_runs.approved = 1`

### Milestone 5.2: Daily digest
- [ ] Runs weekdays after the close: new filings, open positions, triggered exits, notable outcomes

### Milestone 5.3: Signal researcher
- [ ] On a high-score alert: news, earnings dates, and committee context appended to the email

### Milestone 5.4: Filing parser fallback
- [ ] Claude vision extraction for `needs_review` filings, marked lower-confidence

### Milestone 5.5: Strategy analyst & journal reviewer
- [ ] Weekly: leaderboard and backtest review, with watchlist and exit-rule proposals backed by evidence
- [ ] Monthly: our real trades compared with system recommendations and SPY buy-and-hold

**Phase 5 exit:** a month of useful weekly reviews.

---

## Phase 6: Live (small)

### Milestone 6.1: Position tracking
- [x] Log manual buys/sells to `my_positions`, linked to the source trade (app Positions page, M0.5)
- [ ] Show live P&L and the current exit-rule status on the Positions page
- [ ] Check exit rules daily, and email when one triggers
- [ ] Settled-cash (T+1) guard to prevent good-faith violations

### Milestone 6.2: Cloud migration
- [ ] SQLite to Postgres (Supabase/Neon)
- [ ] Point the desktop app at Postgres (add the Postgres JDBC driver; datasource URL from env; the app still runs locally)
- [ ] GitHub Actions: `poll.yml` (30 min), `nightly.yml`, `weekly.yml`, with secrets in Actions
- [ ] Switch to a paid price provider with delisted history (Alpaca/Polygon/Tiingo)

### Milestone 6.3: Live evaluation
- [ ] Manual trading in a small Roth slice
- [ ] Track for 3–6 months against SPY buy-and-hold

**Phase 6 exit:** 3–6 months of live results compared against SPY buy-and-hold.

---

## Backlog (app)

- [ ] "Run pipeline now" button that runs `python -m ...` via `ProcessBuilder` and streams the logs
- [ ] Start the app at login (macOS Login Item)
- [ ] Pipeline health on the Dashboard (last run per job, recent failures); needs a `pipeline_runs` table

---

## Open questions (from design doc)

1. Initial watchlist: follow everyone and let F6 decide, or start with a hand-picked list? *(blocks M1.5)*
2. How much Roth money to allocate in phase 6, and what per-position size limits? *(blocks M6.3)*
3. Include options trades as signals (translated to buying the underlying stock)? *(affects M1.3 / M3.1)*
4. Treat sales as exit signals for our positions, or as bearish signals to avoid a ticker? *(affects M4.1)*
5. Budget for paid price data and a hosted database after phase 2? *(blocks M6.2; ideally decided before Phase 3 conclusions)*
