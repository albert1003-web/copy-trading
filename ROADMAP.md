# Roadmap

Derived from `Congressional Trade Tracker – Design Doc.pdf` (Draft v1, Sep 23, 2026). Each milestone has a concrete exit criterion. Check items off as they land.

Feature key: **F1** alerts · **F2** S&P benchmark · **F3** open-price inflation · **F4** exit timing · **F5** trade scorecard · **F6** member leaderboard · **F7** agent layer

---

## Milestone 0: Project foundation

Goal: an empty but runnable project skeleton.

- [ ] `pyproject.toml` (Python 3.12), dev dependencies (pytest, ruff)
- [ ] Repository layout from `CLAUDE.md` with package `__init__.py` files
- [ ] `.gitignore` (`.env`, `.venv/`, `data/raw/`, `*.db`)
- [ ] `.env.example` (Gmail address, app password, alert recipient, Anthropic API key)
- [ ] `db/schema.sql` for all 11 tables: `members`, `filings`, `trades`, `prices`, `trade_outcomes`, `exit_backtests`, `member_scores`, `watchlist`, `alerts`, `my_positions`, `agent_runs`
- [ ] DB helper: connect, apply schema/migrations (SQLite now, Postgres-compatible SQL)
- [ ] Logging setup (file + console)
- [ ] Fill in the **Commands** section of `CLAUDE.md`

**Done when:** `pytest` runs green on an empty suite, and `python -m db.init` creates the database.

---

## Phase 1: Alerts (F1)

### Milestone 1.1: House ingestion
- [ ] Poll the Clerk's yearly filing index (ZIP/XML) for PTRs (`FilingType = P`)
- [ ] Download new PTR PDFs to `data/raw/house/<doc_id>.pdf`
- [ ] Insert into `filings` with `first_seen_at` and `parse_status = pending`
- [ ] Polite client: User-Agent, backoff, ≥30 min interval
- [ ] Measure index lag against the live search page, and switch sources if the index lags

**Done when:** a run picks up all new House PTRs with no duplicates on re-run.

### Milestone 1.2: Senate ingestion
- [ ] Requests session that accepts the eFD terms agreement (CSRF token)
- [ ] Search PTRs by date range and fetch each report's HTML
- [ ] Cache HTML to `data/raw/senate/<doc_id>.html`, insert into `filings`

**Done when:** a run picks up all new Senate PTRs with no duplicates on re-run.

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
- [ ] Initial watchlist (see open question 1)
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

### Milestone 3.4: Dashboard
- [ ] Streamlit (read-only): leaderboard, outcomes, inflation charts, recent filings

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
- [ ] Proposal format (structured output) and a human-approval flow

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
- [ ] Log manual buys to `my_positions`, linked to the source trade
- [ ] Check exit rules daily, and email when one triggers
- [ ] Settled-cash (T+1) guard to prevent good-faith violations

### Milestone 6.2: Cloud migration
- [ ] SQLite to Postgres (Supabase/Neon)
- [ ] GitHub Actions: `poll.yml` (30 min), `nightly.yml`, `weekly.yml`, with secrets in Actions
- [ ] Switch to a paid price provider with delisted history (Alpaca/Polygon/Tiingo)

### Milestone 6.3: Live evaluation
- [ ] Manual trading in a small Roth slice
- [ ] Track for 3–6 months against SPY buy-and-hold

**Phase 6 exit:** 3–6 months of live results compared against SPY buy-and-hold.

---

## Open questions (from design doc)

1. Initial watchlist: follow everyone and let F6 decide, or start with a hand-picked list? *(blocks M1.5)*
2. How much Roth money to allocate in phase 6, and what per-position size limits? *(blocks M6.3)*
3. Include options trades as signals (translated to buying the underlying stock)? *(affects M1.3 / M3.1)*
4. Treat sales as exit signals for our positions, or as bearish signals to avoid a ticker? *(affects M4.1)*
5. Budget for paid price data and a hosted database after phase 2? *(blocks M6.2; ideally decided before Phase 3 conclusions)*
