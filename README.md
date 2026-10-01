# Congressional Trade Tracker

A personal system that monitors public stock-trade disclosures (Periodic Transaction Reports) from members of the U.S. House and Senate. It emails alerts when followed members trade and measures how those trades perform against the S&P 500 **from the moment they become public**.

The analytics answer three questions:

1. **Which trades are worth copying?**
2. **When should we sell?**
3. **Whom should we follow?**

> The system recommends; a human decides and executes. Trades are placed by hand in a Fidelity Roth IRA. There is no automated order execution.

## Status

🚧 **Early development.** The desktop app shell and the database schema are built. The data pipelines are next. See [`ROADMAP.md`](ROADMAP.md) for milestones and progress.

## Features

| # | Feature | Output |
|---|---------|--------|
| F1 | **Trade alerts**: detect new House/Senate filings, parse trades, filter to the watchlist, email a scored summary | Email alert |
| F2 | **S&P benchmark**: abnormal return vs SPY at 1/5/10/20/60 trading days after disclosure | `trade_outcomes` |
| F3 | **Open-price inflation**: detect a copy-trader "pop" at the open after disclosure | Best entry delay |
| F4 | **Exit timing**: walk-forward backtests of exit rules | Recommended exit + confidence |
| F5 | **Trade scorecard**: win/loss vs SPY for every disclosed trade and our own | Win/loss history |
| F6 | **Member leaderboard**: shrinkage-adjusted ranking of members by post-disclosure returns | Watchlist recommendations |
| F7 | **Agent layer**: Claude agents that summarize, research, and propose strategy changes for human approval | Daily digest, weekly review |

## Architecture

Small, independent pipelines share one database. Each stage can be re-run or replaced on its own.

```
[House Clerk]   [Senate eFD]
        \         /
   (1) Ingestion ── every 30 min, weekdays
          |
   (2) Parse & normalize ── PDF/HTML -> trade rows (LLM fallback for scanned PDFs)
          |
   (3) Enrich ── tickers, sector, committees, filing delay
          |
   (4) Scoring & alerting ──> Gmail
          |
   (5) Price pipeline ── nightly OHLCV for traded tickers + SPY
          |
   (6) Analytics ── nightly outcomes, open inflation, exit backtests, leaderboard
          |
   (7) Agents ── daily digest, weekly strategy review, ad-hoc research
          |
   (8) Desktop app ── Spring Boot + React, runs locally on 127.0.0.1
```

## Tech stack

Python 3.12 · httpx/requests · pdfplumber · BeautifulSoup · SQLite → Postgres (Supabase/Neon) · pandas + DuckDB · yfinance → Alpaca/Polygon/Tiingo · cron → GitHub Actions · Gmail SMTP · Anthropic Python SDK · pytest

**Desktop app:** Spring Boot 3.3 (Java 17) · SQLite JDBC · React 18 + TypeScript + Vite · packaged with `jpackage`

## Running the app

`Trade Tracker.app` is a local macOS app. Opening it starts a small server on your laptop and opens the UI in your browser at http://localhost:8787. Nothing is reachable from the network. Use the **Quit** button in the UI to stop it.

```bash
cd app
./package-mac.sh --install     # builds Trade Tracker.app and copies it to ~/Applications
```

Then open **Trade Tracker** from `~/Applications` (or Spotlight). Requirements to *build*: Java 17+ and Maven (Node is downloaded automatically). To *run*: nothing, because the app bundles its own Java runtime.

Data lives in `~/TradeTracker/tracker.db`. Set `TRACKER_DB_PATH` to use a different file. The app shows empty pages until the pipelines below start filling the database.

| Page | What it's for |
|------|---------------|
| Dashboard | Counts, recent alerts, recent filings |
| Trades | Every disclosed trade, filterable by member, ticker, and action |
| Watchlist | Choose whose trades trigger alerts |
| Leaderboard | Members ranked by post-disclosure abnormal return |
| Positions | Record the trades you placed by hand (it never places orders) |
| Agents | Read agent digests and approve or reject their proposals |

For development with hot reload, see the Commands section in [`CLAUDE.md`](CLAUDE.md).

## Pipelines (getting started)

The Python pipeline setup lands with Milestone 0. The planned flow:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # fill in Gmail app password, recipient, Anthropic API key
pytest
```

## Documentation

- [`Congressional Trade Tracker – Design Doc.pdf`](<Congressional Trade Tracker – Design Doc.pdf>): full design
- [`ROADMAP.md`](ROADMAP.md): phased milestones with exit criteria
- [`CLAUDE.md`](CLAUDE.md): conventions, definitions, and hard rules for contributors (and Claude Code)

## Important notes

- **Personal research only.** Congressional disclosure data may not be resold or used commercially. This project does not publish the data.
- **Not financial advice.** Disclosure lags can erase any edge. All metrics are measured from the disclosure date to answer exactly that question.
- **Secrets** live in `.env` (gitignored) or GitHub Actions secrets. Never commit them.
