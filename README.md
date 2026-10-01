# Congressional Trade Tracker

A personal system that monitors public stock-trade disclosures (Periodic Transaction Reports) from members of the U.S. House and Senate. It emails alerts when followed members trade and measures how those trades perform against the S&P 500 **from the moment they become public**.

The analytics answer three questions:

1. **Which trades are worth copying?**
2. **When should we sell?**
3. **Whom should we follow?**

> The system recommends; a human decides and executes. Trades are placed by hand in a Fidelity Roth IRA. There is no automated order execution.

## Status

🚧 **Pre-development.** The design is complete and implementation hasn't started. See [`ROADMAP.md`](ROADMAP.md) for milestones and progress.

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
   (8) Dashboard ── Streamlit, read-only
```

## Tech stack

Python 3.12 · httpx/requests · pdfplumber · BeautifulSoup · SQLite → Postgres (Supabase/Neon) · pandas + DuckDB · yfinance → Alpaca/Polygon/Tiingo · cron → GitHub Actions · Gmail SMTP · Anthropic Python SDK · Streamlit · pytest

## Getting started

Setup instructions will land with Milestone 0. The planned flow:

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
