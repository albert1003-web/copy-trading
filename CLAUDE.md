# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**Congressional Trade Tracker.** A personal system that watches public stock-trade disclosures (PTRs) from members of Congress, emails alerts when members we follow trade, and measures how those trades perform *after they become public*. The analytics feed an agent layer that proposes strategy changes.

- Source of truth for the design: `Congressional Trade Tracker – Design Doc.pdf` (repo root).
- Plan and progress: `ROADMAP.md`. Update its checkboxes when a milestone task lands.

### Hard rules (do not violate)

1. **No automated trading.** The system recommends; a human executes trades by hand in a Fidelity Roth IRA. Never add brokerage order code.
2. **Agents are read-only.** Agent tools get read-only DB access plus web search. Agents write *proposals* that a human approves. They never change rules, the watchlist, or positions directly.
3. **Measure from disclosure, not the trade date.** Every tradeable metric starts at D0 (see Definitions). Returns from the trade date are for context only and must never feed a signal or a score.
4. **No secrets in git.** Use `.env` locally and GitHub Actions secrets in CI. `.env` must stay in `.gitignore`.
5. **Personal use only.** Never add features that publish or resell disclosure data (disclosure law restricts commercial use).
6. **Polite scraping.** Poll no more often than every 30 minutes, send a descriptive User-Agent, back off on errors, and cache raw files so parsing re-runs never re-download.
7. **Roth constraints.** No shorting, margin, or options execution. Suggestions must respect settled cash (T+1) so they never cause a good-faith violation.

## Architecture

Independent pipeline stages share one database. Each stage reads the previous stage's tables and writes its own, so any stage can be re-run or replaced alone. Keep stages decoupled: no stage should import another stage's internals. Communicate through tables.

```
(1) ingest    House Clerk index + Senate eFD -> filings (+ raw files on disk)
(2) parse     PDF/HTML -> trades (LLM vision fallback for scanned filings)
(3) enrich    ticker validation, sector, committees, filing_delay_days
(4) alerts    scoring -> Gmail; position exit watching
(5) prices    nightly OHLCV for traded tickers + SPY
(6) analytics nightly outcomes, open inflation, exit backtests, leaderboard
(7) agents    daily digest, weekly strategy review, ad-hoc research
(8) dashboard Streamlit, read-only
```

### Repository layout (target)

```
ingest/      house.py, senate.py
parse/       house_pdf.py, senate_html.py, llm_fallback.py
enrich/      tickers.py, committees.py
prices/      fetch.py
analytics/   outcomes.py, open_inflation.py, exits.py, leaderboard.py
alerts/      score.py, email.py, positions.py
agents/      tools.py, digest.py, researcher.py, strategist.py
dashboard/   app.py
db/          schema.sql, migrations/
tests/       fixtures/, test_parsers.py
.github/workflows/  poll.yml (30 min), nightly.yml, weekly.yml
data/raw/    cached PDFs/HTML keyed by doc_id (gitignored)
```

## Stack

- Python 3.12
- Scraping: httpx/requests, pdfplumber, BeautifulSoup
- Storage: SQLite (phase 1), then Postgres on Supabase/Neon. Write portable SQL and avoid SQLite-only features.
- Analytics: pandas + DuckDB (vectorbt optional for exit grids)
- Prices: yfinance to start, then Alpaca/Polygon/Tiingo before trusting results
- Scheduling: local cron, then GitHub Actions cron
- Alerts: Gmail SMTP with an app password
- Agents: Anthropic Python SDK with tool use. Default to the latest capable Claude model.
- Dashboard: Streamlit
- Tests: pytest with saved sample filings in `tests/fixtures/`

## Commands

Tooling isn't set up yet. Fill this in during Milestone 0.

```bash
# python -m venv .venv && source .venv/bin/activate
# pip install -e ".[dev]"
# pytest
# python -m ingest.house        # one ingestion pass
```

## Definitions (use these exactly)

- **D0**: the first trading-day open after we *first saw* the filing (`filings.first_seen_at`). This is the earliest realistic entry.
- **Return(h)**: price at D0 + h trading days / D0 open − 1, for h ∈ {1, 5, 10, 20, 60}.
- **Abnormal return(h)**: Return(h) − SPY return over the same window.
- **Win**: abnormal return > 0 at the evaluated horizon (or under the chosen exit rule).
- **Open inflation(k)**: Open(D0) / Open(D0 + k) − 1, for k ∈ {1, 2, 3, 5}. Positive means buying k days later was cheaper.
- A trade is **complete** after 60 trading days.
- Use **adjusted closes** for returns and **raw opens** for entry-price analysis.

## Normalized enums

- `action`: `BUY | SELL | SELL_PARTIAL | EXCHANGE`
- `owner`: `self | spouse | joint | dependent`
- `asset_type`: `stock | option | other`
- Amount ranges are stored as integer `amount_min` / `amount_max`.
- Low-quality parses carry a `confidence` value. Unparseable filings get `parse_status = needs_review`.

## Conventions

- **Parsers need fixtures.** Every parser change comes with a saved sample filing in `tests/fixtures/` and a test. Source formats change without warning, and the fixtures catch it.
- **Idempotent jobs.** Every job must be safe to re-run. Upsert by natural keys (`doc_id`, `(ticker, date)`, etc.).
- **Backtests report out-of-sample results only.** Walk-forward: train on a rolling 2 years, test on the next quarter. Include costs, slippage, and the Roth cash-settlement constraint. Keep parameter grids small.
- **Leaderboard.** Use shrinkage (empirical Bayes) scores and a minimum sample size, so a member with 3 lucky trades can't top the list.
- **Survivorship bias.** Delisted tickers are missing from free price data. Flag any conclusions drawn before the switch to a paid provider.
- **Agent auditing.** Log every agent run (prompt, tools called, output, approval) to `agent_runs`.
- Log to file, and alert by email on repeated pipeline failures.
- Keep modules small and plain. Prefer simple functions over frameworks.
