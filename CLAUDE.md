# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**Congressional Trade Tracker.** A personal system that watches public stock-trade disclosures (PTRs) from members of Congress, emails alerts when members we follow trade, and measures how those trades perform *after they become public*. The analytics feed an agent layer that proposes strategy changes.

The system has two halves that share one SQLite database:
- **Python pipelines** (ingest → parse → enrich → alerts → prices → analytics → agents). These run on a schedule and fill the DB.
- **Desktop app** (`app/`, Spring Boot + React). A local macOS app the user double-clicks to browse the data and record the few things they do by hand.

- Source of truth for the design: `Congressional Trade Tracker – Design Doc.pdf` (repo root). The desktop app replaces the doc's Streamlit dashboard.
- Plan and progress: `ROADMAP.md`. Update its checkboxes when a milestone task lands.

### Hard rules (do not violate)

1. **No automated trading.** The system recommends; a human executes trades by hand in a Fidelity Roth IRA. Never add brokerage order code, in Python or in the app. The Positions page only *records* trades.
2. **Agents are read-only.** Agent tools get read-only DB access plus web search. Agents write *proposals* that a human approves in the app. They never change rules, the watchlist, or positions directly.
3. **Measure from disclosure, not the trade date.** Every tradeable metric starts at D0 (see Definitions). Returns from the trade date are for context only and must never feed a signal or a score.
4. **No secrets in git.** Use `.env` locally and GitHub Actions secrets in CI. `.env` must stay in `.gitignore`.
5. **Personal use only.** Never add features that publish or resell disclosure data (disclosure law restricts commercial use).
6. **Polite scraping.** Poll no more often than every 30 minutes, send a descriptive User-Agent, back off on errors, and cache raw files so parsing re-runs never re-download.
7. **Roth constraints.** No shorting, margin, or options execution. Suggestions must respect settled cash (T+1) so they never cause a good-faith violation.
8. **The app stays local.** It binds to `127.0.0.1` and has no login. Never change `server.address` or expose it on the network.

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
(8) app       Spring Boot + React desktop app (see below)
```

### Desktop app (`app/`)

One process: Spring Boot serves the REST API (`/api/*`) and the built React files from a single jar. `jpackage` wraps that jar plus a bundled Java runtime into `Trade Tracker.app`. Opening the app starts the server on `127.0.0.1:8787` and shows the UI in its own window.

- **App window** (`Browser.java`). The app launches Chrome (or Edge/Brave/Chromium) in `--app` mode with its own profile in `~/TradeTracker/window-profile`, so there are no tabs or address bar. Quitting the server closes the window. Closing the window quits the server only if that Chrome process exits; on macOS Chrome usually keeps running after its last window closes, so the server stays up in the background. Double-clicking the app while it runs opens a new window (macOS sends a "reopen" event, handled in `Browser`, instead of starting a second process). Without a Chromium browser it falls back to a tab in the default browser.
- **Single instance.** If port 8787 is already taken, launching the app only opens a window.
- **Quit.** Close the window, or use the UI's Quit button (`POST /api/shutdown`).
- **Packaging.** `package-mac.sh` adds the icon (`packaging/TradeTracker.icns`, drawn by `packaging/make-icon.swift`; delete the `.icns` to regenerate it). It sets `LSUIElement` so the background server stays out of the Dock, then re-signs ad hoc. If the app is running, it's quit via `/api/shutdown` after the jar builds (a failed build leaves it running), and `--install` reopens it.
- **Data access.** Plain `JdbcTemplate` SQL, with one repository class per area in `com.tracker.repo`. No JPA: the schema belongs to `db/schema.sql`, not to Java entities. Reads return `List<Map<String, Object>>` straight to JSON.
- **Frontend.** React + TypeScript + Vite in `app/frontend/`. There's no state library: pages use the `useApi(path)` hook (`src/hooks.ts`), the `api` wrapper (`src/api.ts`), and the shared `Table` component (`src/components.tsx`).
- **Charts.** `src/charts.tsx` has `ColumnChart` and `LineChart`, hand-drawn SVG with no chart library:
  - thin columns with a rounded data end, 2px lines, hairline grid;
  - a legend for 2+ series, and a hover/focus tooltip;
  - colors from `--series-1`/`--series-2` in `styles.css`, validated for the light and dark surfaces.
  Every chart sits next to a table with the same numbers.
- **Positions page:**
  - the exit-rule dropdown lists `exit_rules` (`GET /api/exit-rules`, `ExitRulesController`), with the recommended rule pre-selected, plus "Don't watch"; it stores the rule's label in `my_positions.exit_rule`;
  - without `exit_rules` it falls back to a free-text input;
  - the table tags each position "watching", "triggered <date>" (from `exit_alerts`) or "not watched".
- **Analytics pages** (`AnalyticsController` + `AnalyticsRepository`) read the analytics tables:
  - Leaderboard: `/api/leaderboard?horizon=`, from `member_horizon_stats`, ranked like `analytics.leaderboard` (≥ 20 filings, by shrunk score).
  - Outcomes: `/api/outcomes/summary|members|trades`, per-filing averages computed in SQL.
  - Open inflation: `/api/open-inflation`.
  - Trade detail: `/trades/:id`, from `/api/trades/{id}` and `/api/trades/{id}/prices` (the symbol and SPY indexed to the D0 adjusted open).
- **Agents page** (`AgentRunsController`): `/api/agent-runs` returns runs with their `proposals` (evidence parsed from JSON).
  - Each proposal is approved or rejected on its own (`POST /api/agent-proposals/{id}/decision`). The decision can change until the pipeline applies it, then it's 409.
  - Runs without proposals keep the run-level decision (`/api/agent-runs/{id}/decision`).
- **SPA routing.** `SpaConfig` sends unknown non-`/api` paths to `index.html`.

### Database ownership

- **One schema file:** `db/schema.sql` (idempotent `CREATE TABLE IF NOT EXISTS`) is always the *full current* schema; fresh databases are built from it. The app copies it onto its classpath at build time and applies it on startup. Python applies the same file. Don't define tables anywhere else.
- **Migrations:** every schema change *also* gets `db/migrations/NNN_name.sql` to upgrade existing databases.
  - Both `db/__init__.py` (Python) and `MigrationRunner.java` (app) apply files numbered above `PRAGMA user_version`, ignoring "duplicate column name". Whichever side opens the DB first upgrades it.
  - Write one statement per `;`-terminated line group, and keep migrations additive (`ADD COLUMN`, `CREATE TABLE IF NOT EXISTS`).
  - Update `tests/fixtures/schema_v0.sql` only if the baseline changes.
- **Data location:** everything lives in `~/TradeTracker/`, outside the repo:
  - `tracker.db` (override with `TRACKER_DB_PATH`, honored by both the app and the pipelines)
  - `raw/` cached source files (`TRACKER_RAW_DIR`)
  - `logs/pipeline.log` (`TRACKER_LOG_DIR`)
  - `filings.raw_path` is relative to `raw/`, e.g. `house/2026/20035528.pdf`.
- **The app writes only user-owned data:** `watchlist`, `my_positions`, `agent_runs.approved` and `agent_proposals.approved/decided_at`. Every other table is read-only from the app.
  - The only pipeline writer of `watchlist` is `agents/apply.py`, which carries out proposals you approved.
- SQLite runs in WAL mode with `busy_timeout`, so the app and the pipelines can run at the same time.

### Repository layout

```
app/                         Desktop app (built)
  pom.xml                    Spring Boot 3.3, Java 17, sqlite-jdbc, frontend-maven-plugin
  package-mac.sh             -> app/dist/Trade Tracker.app (--install: ~/Applications + Desktop shortcut)
  packaging/                 app icon (.icns) and its generator script
  src/main/java/com/tracker/
    TrackerApplication.java  main, single-instance check
    Browser.java             opens the app window; window close <-> app quit
    SpaConfig.java           static files + SPA fallback
    api/                     REST controllers
    repo/                    JdbcTemplate repositories
  frontend/src/              App.tsx (nav/routes), pages/, api.ts, hooks.ts, components.tsx, charts.tsx, format.ts
common/      config.py (paths/env), http.py (polite client: UA, retries, pauses), log.py, signals.py (trade features
             shared by analytics/factors.py and the alert score), exit_rules.py (exit rules + daily-bar simulator,
             shared by analytics/exits.py and alerts/positions.py)
db/          __init__.py (connect + migrations), init.py, schema.sql, migrations/
pipeline/    run.py (one scheduled pass of every stage + nightly stages), schedule.py (launchd), backfill.py (history),
             report.py (coverage, detection latency, review queue, price gaps)
ingest/      house.py, senate.py, available.py (available_at: live vs backfilled)
parse/       normalize.py (enums, amounts, tickers), house_pdf.py, senate_html.py, run.py; llm_fallback.py (planned)
enrich/      reference.py (cached legislators, committees, symbol lists), members.py, tickers.py, securities.py
             (sector/industry/size), committees.py (per Congress + committee_relevant), run.py,
             member_aliases.csv, ticker_aliases.csv, committee_snapshots.csv, committee_sectors.csv
prices/      fetch.py (Yahoo daily bars, coverage, gap report)
analytics/   outcomes.py (D0, returns, abnormal returns, wins), open_inflation.py (+ high_attention_members.csv),
             stats.py (empirical-Bayes shrinkage), leaderboard.py (member_scores), factors.py (signal_factors);
             exits.py (costs, T+1 ledger, walk-forward -> exit_backtests, recommendation -> exit_rules)
alerts/      score.py (v1/v2 score), rules.py (what qualifies), email.py (compose + Gmail), run.py,
             positions.py (exit watcher for the positions you logged)
agents/      tools.py (read-only DB tools + web search), mcp_server.py (the same tools over MCP), proposals.py (output
             schema, validation), runner.py (run + agent_runs logging; API backend), claude_code.py (Claude Code
             backend), ask.py (ad-hoc research CLI), watchlist_review.py (rule-based proposals), apply.py (approved
             proposals -> watchlist), digest.py (daily digest), researcher.py (research briefs for high-score
             alerts); strategist.py (planned)
tests/       conftest.py (temp DB, FakeHouseClerk / FakeSenateEfd via httpx.MockTransport), test_db.py,
             test_house_ingest.py, test_senate_ingest.py, test_normalize.py, test_parse_fixtures.py,
             test_parse_run.py, test_backfill.py, test_prices.py, test_outcomes.py, test_open_inflation.py, test_leaderboard.py, test_exits.py, test_agents.py, fixtures/ (house/electronic_*.pdf + senate/ptr_*.html, each with .expected.json)
.github/workflows/  poll.yml (30 min), nightly.yml, weekly.yml (planned, M6.2)
```

## Stack

- **Pipelines:** Python 3.12
- **Scraping:** httpx/requests, pdfplumber, BeautifulSoup
- **Storage:** SQLite (phase 1), then Postgres on Supabase/Neon. Write portable SQL and avoid SQLite-only features.
- **Analytics:** pandas + DuckDB (vectorbt optional for exit grids)
- **Prices:** yfinance to start, then Alpaca/Polygon/Tiingo before trusting results
- **Scheduling:** local cron, then GitHub Actions cron
- **Alerts:** Gmail SMTP with an app password
- **Agents:** the Claude Code CLI (`claude -p`, on the Claude subscription; tools over MCP) by default, or the Anthropic Python SDK with tool use (`--api`). Default to the latest capable Claude model.
- **App backend:** Spring Boot 3.3 on Java 17, `spring-boot-starter-jdbc`, `org.xerial:sqlite-jdbc`
- **App frontend:** React 18, TypeScript, Vite 5, react-router 6, plain CSS (`src/styles.css`, light/dark via CSS variables)
- **Tests:** pytest with saved sample filings in `tests/fixtures/`

## Commands

### Desktop app

```bash
cd app
mvn test                             # backend (JUnit) + frontend (Vitest) tests
mvn package                          # tests, then build frontend + backend -> target/trade-tracker.jar
java -jar target/trade-tracker.jar   # run it; opens the app window at http://localhost:8787
./package-mac.sh                     # build dist/Trade Tracker.app (bundled Java runtime)
./package-mac.sh --install           # ...install to ~/Applications + shortcut on the Desktop

# Development (hot reload): run both, then open http://localhost:5173
mvn spring-boot:run -Dspring-boot.run.arguments=--tracker.open-browser=false
cd frontend && npm run dev           # Vite proxies /api -> 127.0.0.1:8787
cd frontend && npm test              # frontend tests only (vitest run)

# Use a throwaway DB instead of ~/TradeTracker/tracker.db
TRACKER_DB_PATH=/tmp/test.db java -jar target/trade-tracker.jar --tracker.open-browser=false
```

### Python pipelines

```bash
/opt/homebrew/bin/python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                 # optional settings (TRACKER_CONTACT, later Gmail/Anthropic keys)

pytest                               # all Python tests (no network)
ruff check .
python -m db.init                    # create/upgrade ~/TradeTracker/tracker.db

python -m ingest.house               # one House pass: index + search page -> filings, then download PDFs
python -m ingest.house --no-download # record filings only
python -m ingest.house --year 2025   # a specific filing year (repeatable)
python -m ingest.house --lag-report  # does the daily index lag the live search page?
python -m ingest.senate              # one Senate pass: eFD search (from last run - 7 days) -> filings, cache HTML
python -m ingest.senate --since 2026-01-01   # search from a given received date (backfill)
python -m ingest.senate --no-download       # record filings only
python -m parse.run                  # parse pending electronic filings from the raw cache -> trades
python -m parse.run --reparse        # re-parse every electronic filing (after a parser fix); trade_ids stay stable
python -m parse.run --doc-id 20035528 --chamber house   # one filing (repeatable) / one chamber
python -m enrich.run                 # members, filer -> member, ticker validation, filing delay (re-run any time)
python -m enrich.run --offline       # use the cached reference files only
python -m alerts.email --test        # check the Gmail settings in .env
python -m alerts.run                 # email new watchlist trades (the first run only sets the start time)
python -m alerts.run --dry-run --since 2026-09-01   # print what would be sent; writes nothing (no research)
python -m alerts.run --dry-run --since 2026-09-01 --research   # ...with research briefs (uses Claude; logs the runs)
python -m alerts.run --no-research   # send without research briefs
python -m pipeline.run               # one full pass (ingest -> parse -> enrich -> alerts), if due
python -m pipeline.run --force       # ...even if the last run was under 30 min (2 h on weekends) ago
python -m pipeline.run --force --nightly   # ...and the nightly stages (prices, analytics, securities) now
python -m pipeline.backfill --from 2020    # history through last year: both chambers, then parse (resumable)
python -m pipeline.backfill --from 2021 --to 2021 --chamber house   # one year / chamber
python -m prices.fetch               # daily bars for active symbols + SPY (the nightly stage does this)
python -m prices.fetch --all         # every traded symbol; --symbol X for one
python -m prices.fetch --gaps        # coverage and gap report (missing/partial symbols)
python -m prices.fetch --refetch-partial   # refetch full history for symbols whose bars start late (repair)
python -m analytics.outcomes         # recompute trade_outcomes for every priced trade (the nightly stage does this)
python -m analytics.outcomes --report   # coverage by year + BUY abnormal returns / hit rate per horizon
python -m analytics.open_inflation   # open_infl_k + aggregates + best entry delays (after outcomes; nightly does this)
python -m analytics.open_inflation --report   # by market cap, media attention and member
python -m analytics.leaderboard      # today's member_scores snapshot (after outcomes; nightly does this)
python -m analytics.leaderboard --report --horizon 60   # ranked members: avg return vs S&P 500, hit rate, score
python -m analytics.factors          # v2 alert score's feature effects (--report prints them)
python -m analytics.exits            # walk-forward exit backtests -> exit_backtests (nightly does this; --report prints)
python -m analytics.exits --check    # exit engine sanity: zero-cost fixed holds == trade_outcomes, settled cash >= 0
python -m agents.ask "Which ranked member not on my watchlist has the best 20-day record?"   # logged; review in the app
python -m agents.ask --no-web --dry-run "..."   # database only; print the answer, write nothing
python -m agents.ask --api "..."     # the Anthropic API (ANTHROPIC_API_KEY) instead of Claude Code
python -m agents.watchlist_review --dry-run   # rule-based watchlist proposals (nightly does this, without --dry-run)
python -m agents.apply               # apply approved watchlist proposals now (every pipeline run does this)
python -m agents.digest --dry-run    # today's digest, printed only (--template: no model; the pipeline runs it daily)
python -m pipeline.schedule install  # run it every 30 min via launchd (also: uninstall, status)
python -m pipeline.report            # coverage gaps, detection lag, House index vs search, alert latency

# Throwaway run that leaves real data alone
TRACKER_DB_PATH=/tmp/t.db TRACKER_RAW_DIR=/tmp/raw TRACKER_LOG_DIR=/tmp/logs python -m ingest.house
```

## Sources

### House (Clerk), `ingest/house.py`
- **Index:** `public_disc/financial-pdfs/{year}FD.zip`, which holds `{year}FD.xml`. Each `<Member>` has `Prefix, Last, First, Suffix, FilingType, StateDst, Year, FilingDate (M/D/YYYY), DocID`; PTRs are `FilingType=P`.
  - It's rebuilt about once a day (~9:00 ET), so we poll it with a conditional GET (ETag), which usually returns 304.
- **Search page:** `POST FinancialDisclosure/ViewMemberSearchResult` with `FilingYear`.
  - It returns every filing for the year, with name, office, year, type (`PTR Original` / `PTR Amendment` / ...) and the PDF link, but **no filing date**.
  - We poll it too, in case it lists filings before the index. `--lag-report` measures this.
- **PDFs:** `public_disc/ptr-pdfs/{year}/{DocID}.pdf`.
  - DocIDs starting with `2` are electronic (text layer). DocIDs starting with `8` or `9` are scanned paper.
  - Format is confirmed after download by checking for `/Font`. Scanned filings get `parse_status = needs_review`.
- Filers aren't resolved to `members` yet (M1.4): `filings.member_id` is NULL, and `filer_name` / `state_district` hold the source values.

### Senate (eFD), `ingest/senate.py`
- **Terms agreement:** every request needs a session that has accepted the agreement. GET `/search/home/` for the form's `csrfmiddlewaretoken`, then POST it with `prohibition_agreement=1`. Later requests send the `csrftoken` cookie as `X-CSRFToken` and as the form field. A request without an accepted session is redirected to `/search/home/`; the client then accepts again once.
- **Search:** POST `/search/report/data/` (DataTables JSON) with `report_types=[11]` (PTR), `filer_types=[1,5]` (Senator, Former Senator), `submitted_start_date` (`MM/DD/YYYY HH:MM:SS`), `start`/`length` (pages of 100), ordered by date received.
  - Each row is `[first, last, office, '<a href="/search/view/{ptr|paper}/<uuid>/">title</a>', date received]`. Amendments say so in the title.
  - The window starts 7 days before the last successful search (`source_state.senate_search`). On the first run it starts Jan 1.
- **Reports:** `doc_id` is the UUID. `/search/view/ptr/<uuid>/` is an electronic HTML table (`pending`). `/search/view/paper/<uuid>/` is a page of scanned GIFs (`scanned`, `needs_review`); only its HTML is cached for now.
  - Cached at `raw/senate/<year>/<uuid>.html`. A page is validated before it's written, so the agreement form is never cached as a report.
- eFD results carry no state, so `state_district` is NULL for the Senate.

### Parsing, `parse/`
- **Input:** electronic filings with a cached raw file, read from disk only (never the network). Scans stay `needs_review` for the M5.4 vision fallback.
- **House PDF:** the Transactions table's columns are found by the header words' x-positions on each page.
  - A row starts on a line with a type, a date, and a notification date in their columns. The asset name and amount can wrap onto later lines.
  - Small-caps detail lines (`Filing Status`, `Subholding Of`, `Location`, `Description`, `Comments`) extract with `\x00` padding, e.g. `F\x00\x00 S\x00:`. Description + Comments go to `trades.description`.
  - Owner `SP/JT/DC/blank`, type `P/S/S (partial)/E`. The asset code is `[ST]` stock, `[OP]` option, anything else other. The ticker is the last `(TICKER)` in the asset name.
  - The table ends at the `* For the complete list of asset type abbreviations` footnote.
  - Older filings (2020–23) use a font whose capitals often extract as lowercase (`s (partial)`, `[sT]`, `(Dg)`, `FIlINg STATuS:`). Header words and types match case-insensitively; when the header or a detail label shows that font, codes and tickers are uppercased (`(ROKu)` → `ROKU`).
  - Some early-2020 forms have no `Cap. Gains > $200?` column (the header is found without it) and embed a font whose words extract as junk except their first capital (`Fﾛﾞﾛἠﾙ SϹϹﾟ;:` = `Filing Status:`). Those labels match by initials, and a line of only such words (a section title) ends the table, since these forms have no footnote.
- **Senate HTML:** columns are matched by header text. A `--` ticker falls back to a ticker typed into the asset name (`MRSH - Marsh ...`, `... (TGOPY)`). Owner `Child` maps to `dependent`.
- **Rows** are upserted by `(doc_id, line_no)` (unique index from migration 002, not in `schema.sql`), so `alerts`/`my_positions` references survive a re-parse. `disclosure_date` = `filings.filing_date`.
- **Status:**
  - `parsed`: every row was recognized.
  - `needs_review`: no rows, an unrecognized action/owner/date/amount, or a trade dated after the filing (a filer typo). Rows are still written, with `confidence` 0.5.
  - `failed`: the parser raised an error or the file is missing.
- **Tickers** are as filed. Enrichment validates them into `trades.symbol`.

### Enrichment, `enrich/`
- **Reference files** are cached in `raw/reference/` and re-downloaded only when over 7 days old; a failed refresh uses the cached copy.
  - Members: congress-legislators JSON (current + historical, terms ending 2019 or later).
  - Symbols: Nasdaq Trader `nasdaqlisted.txt` / `otherlisted.txt`.
- **Members:** `member_id` = bioguide id. Upserts never delete (watchlist FKs) and never touch `committees`.
  - House filers match by seat (`state_district` held since 2019) + last name. Senate filers match by last name among senators, with first/middle/nickname as the tiebreak.
  - `member_aliases.csv` overrides both; a bioguide of `-` marks a known non-member (a candidate's PTR). Unmatched filers are logged and make the run exit 1.
- **Tickers:** `trades.ticker` stays as parsed. Enrichment writes `symbol`, `ticker_status`, and `is_etf`:
  - `listed`: on the lists, with `BRK-B` / `BRK/B` normalized to `BRK.B`.
  - `renamed`: resolved through `ticker_aliases.csv` (`old,new,date,until,note`); add a row only after confirming the new symbol is listed and Yahoo's history under it covers the old trades.
  - `delisted`: the alias's `new` is `-`: the company is gone and another security took its ticker, so pricing it as filed would measure the wrong company; `symbol` is NULL (never priced or alerted).
  - `until` (optional): the alias covers only trades dated before it, the day the new owner started trading, so later trades in the new security resolve normally. Leave it blank when the new owner is unlikely to be traded (FB is now an ETF; filers kept writing FB for Meta after the rename).
  - Reused tickers show up as `partial` price coverage starting years after the first trade, or as a list name that doesn't match the filed asset name. Check both after a symbol-list refresh.
  - `unlisted`: OTC or delisted; `symbol` = the ticker as filed.
  - `none`: no ticker; `symbol` is NULL.
- Everything is recomputed on each run, so a `parse.run --reparse` is fixed up by the next `enrich.run`.

### History backfill, `pipeline/backfill.py`
- Runs the normal ingest with `backfill=True` for the given years (House index + search per year; Senate search by date received, ending Dec 31 of `--to`, which defaults to last year so a new filing is always found live and alerted), downloads, then parses.
- New rows get `available_basis = 'filed'` (see D0). A later live poll never rewrites `available_at`, nor does a backfill rewrite a live one.
- Holds `pipeline.lock`, so scheduled runs skip while it works. Resumable: cached files are reused. Run it a year at a time so scheduled runs get in between.
- `pipeline.report` prints the review queue (filings / parsed / scanned / needs_review / failed per chamber and year).

### Sectors and committees, `enrich/securities.py`, `enrich/committees.py`
- **Securities:** the nightly `securities` stage fetches Yahoo quote info into `securities` (never-fetched symbols first; refresh after 90 days, 30 for unknown symbols; at most 300 per run). Funds get sector `ETF`.
- `enrich.run` copies sector/industry onto trades and sets `mcap_bucket` = size at disclosure: `market_cap × close(disclosure) / close(when market_cap was fetched)` (works for multi-class stocks; falls back to today's cap without prices).
- **Committees:** current assignments from congress-legislators (weekly), plus one pinned snapshot per past Congress (`committee_snapshots.csv`, downloaded once). When a new Congress starts, pin a late-term commit for the outgoing one. Subcommittees roll up to the parent. Stored in `committee_memberships`; `members.committees` = current committee names.
- `committee_relevant` = the member, in the Congress of the trade date, sat on a committee whose sector (and industry, if given) in `committee_sectors.csv` matches the trade. Hand-curated; Appropriations/Budget are left out as too broad.

### Prices, `prices/fetch.py`
- **Universe:** trades with a symbol (`listed | renamed | unlisted`), open positions, SPY. Each symbol from 10 days before its earliest trade (floor 2019-12-01).
- **Nightly:** only active symbols (a filing available in the last 150 days), positions, SPY and never-fetched ones; `--all` does everything. Increments refetch the last 5 days.
- **Full fetches:** a new symbol, or a known one (active or not) whose trades now need earlier bars, e.g. after a backfill. `price_coverage.needed_from` is the start last fetched and only moves when the symbol is fetched; writing it for unfetched symbols once hid ~220 truncated histories. `--refetch-partial` repairs histories that start after the first disclosure.
- **Re-basing:** if an overlapping bar's close/adj_close moved (a later split or dividend), the symbol's whole history is refetched and replaced. The latest stored bar is ignored for this (it may be a partial day).
- `adj_close` (split + dividend adjusted) is for returns; `open`/`close` are Yahoo's split-adjusted, dividend-unadjusted values (fine for open-inflation ratios).
- **Coverage:** `price_coverage` per symbol: `partial` = starts after the first disclosure, or ends over a week before SPY (delisted). Free data lacks delisted tickers: flag survivorship bias.

### Outcomes, `analytics/outcomes.py`
- **Scope:** trades with a symbol (`listed | renamed | unlisted`); rows for trades that leave the scope are deleted. The trading calendar is SPY's bars.
- **D0:** `available_at` in ET: that day if it's a trading day and before 09:30, else the next trading day. No row until SPY has the D0 bar.
- **Returns:** `adj_close(D0+h) / adjusted open(D0) − 1`, where the adjusted open is `open × adj_close / close` (puts the dividend-unadjusted open on adj_close's basis); `d0_open` is raw. Abnormal = minus SPY over the same window. Missing bars give NULL (no forward fill).
- **Wins:** `win_h` = `abn_ret_h > 0`, only for BUYs we could copy (stock/other or bought calls; `copyable = 1`). Sales, exchanges and puts get NULL (no shorting in the Roth).
- **Complete:** D0+60 trading days has passed, even if the symbol has no bar (delisted).
- **`tx_ret` / `tx_abn_ret`:** trade-date close (last trading day ≤ `tx_date`) → D0 open. Context only: never a signal or score input.
- Every run recomputes every trade (~2 s) and writes only its own columns, so `open_infl_*` (M3.2) survives.

### Open inflation, `analytics/open_inflation.py`
- **Per trade:** `open_infl_k` = `open(D0) / open(D0+k) − 1` for k ∈ {1, 2, 3, 5}, from raw opens, using `trade_outcomes.d0_date` (runs after `outcomes`). Only those columns are written.
- **Aggregates** (`open_inflation_stats`) cover copyable BUYs only, grouped as `all`, `member`, `mcap` (NULL → `unknown`) and `attention`. Attention is `high` if the member is in `analytics/high_attention_members.csv` (hand-curated; edit freely), else `other`.
- **The unit is the filing.** A filing's trades share D0 and market moves, so each filing is one observation (the mean of its trades). `n` = filings, `n_trades` for context. Counting trades made one 600-trade filing look like a large sample.
- **Shrinkage:** `analytics/stats.shrink` (empirical Bayes, toward the group type's pooled mean), reused by the leaderboard.
- **`entry_delays`:**
  - at least 20 filings: `best_k` = the k with the highest shrunk mean if > 0, else 0 (buy at the D0 open);
  - fewer filings: NULL (fall back to the mcap group, then `all`).
- Both tables are replaced every run (latest only).

### Signal factors, `analytics/factors.py`
- These are the feature half of the v2 alert score: for each level in `common/signals.py` (size, delay, amount, committee overlap, kind), the 20-day excess of copyable buys. The unit is the filing, values are clipped at the 1st/99th percentile, and levels are shrunk within each factor.
- `effect` = the shrunk level − that factor's own pooled mean, not the overall mean. A filing with trades at two levels counts in both, so measuring against the overall mean would add a constant offset; with the factor's own mean, a factor with no reliable spread adds exactly 0.
- Replaced each run.

### Leaderboard, `analytics/leaderboard.py`
- **Input:** copyable BUYs with a member, from D0. A horizon counts once it has matured. SPY's return over the same window = `ret_h − abn_ret_h`.
- **Unit = filing**, as for open inflation. Per member and h (`member_horizon_stats`):
  - `n_filings`, `n_trades`;
  - `mean_ret` (the buys' average return), `mean_spy_ret` (the S&P 500's over the same windows), `mean_abn_ret` (the difference);
  - `median_abn_ret`, `hit_rate` (share of filings that beat SPY);
  - `shrunk_score` (`stats.shrink`).
- **Clipping (score only):** filing excesses are clipped at the 1st/99th percentile of all filings at that h, because one +300% filing inflates the noise estimate until every member shrinks to the same score. The displayed means aren't clipped.
- **`member_scores`** = h = 20, ranked by `shrunk_score` (ties go to the higher mean excess) for members with ≥ 20 filings; `rank` is NULL below that. `consistency` = share of years (≥ 3 filings) with a positive mean excess, NULL with < 2 such years.
- **Snapshots:** `as_of` = the ET date. A same-day re-run replaces that day; earlier days are kept. The app shows the latest, ranked first.

### Exit backtests, `analytics/exits.py`
- **Out-of-sample only.** The engine (M4.1) reports no rule performance by itself; only the walk-forward (M4.2) results are stored, in `exit_backtests` (nightly stage `exits`).
- **Shared code:** the rules, `simulate`, `label`/`parse_rule`/`describe` live in `common/exit_rules.py` (re-exported by `exits.py`), so `alerts/positions.py` uses them without importing analytics.
- **Recommendation (M4.3):**
  - `recommend()` = `choose()` trained on the last 2 years of trades that have exited (still-open ones don't count);
  - confidence comes from the walk-forward's out-of-sample record: `high` = beat ≥ 4 of 5 holds with positive excess and ≥ 100 filings, `medium` = ≥ 3 of 5 and ≥ 50 filings, else `low`;
  - written to `exit_rules` (every grid rule, its current training score, exactly one `recommended`), which the Positions form and the buy emails read.
- **Prices:** adjusted bars, where open/high/low = raw × `adj_close / close` (the D0-open basis `outcomes` uses) and close = `adj_close`. No forward fill.
- **Entry:** the adjusted D0 open (`trade_outcomes.d0_date`) for copyable BUYs, one signal per (filing, symbol).
- **Rules:** `FixedHold(days)`, `StopTarget(stop, target)`, `TrailingStop(pct)`, `AtrStop(mult, n)` (chandelier; ATR from bars before D0 only), `MemberSale`. Every rule also exits at the close after `max_hold` = 60 trading days.
- **Fills (conservative):**
  - an open through a stop or target fills at the open;
  - a stop and a target both inside one day's range count as the stop;
  - intraday fills are at the level;
  - a trailing high updates after that day's check;
  - a missing bar on the exit day moves the exit to the next open.
- **`MemberSale`** exits at the open on the D0 of the member's next sale filing of the symbol: the disclosure, never the sale's trade date.
- **Exit reasons:** `data_end` = the bars stopped (delisted), so the exit is at the last close. `open` = the calendar ended first; such trades are left out of trade stats.
- **Costs:**
  - commission in dollars per order (default $0);
  - slippage per side by `mcap_bucket` (bps: mega 5, large 10, mid 20, small 40, micro/unknown 75);
  - abnormal return = net return − SPY over the same window, with no costs on SPY.
- **Portfolio (Roth T+1):**
  - size = 5% of the previous close's equity, at most 20 positions, one position per symbol;
  - buys use settled cash only, and proceeds settle the next trading day, so a good-faith violation is impossible; a short signal is skipped and counted;
  - day order: open exits, then buys, then intraday/close exits.
- **Universe:** `all | watchlist | ranked | {members}`.
  - `ranked` is point-in-time: at each quarter start, the leaderboard's rules (≥ 20 filings, shrunk score > 0) are applied to filings whose D0 + 20 had passed, via `leaderboard.filings(d0_before=)`.
  - `watchlist` is today's, so it carries hindsight.
- **`--check`:** zero-cost `FixedHold(h)` must reproduce `trade_outcomes.ret_h/abn_ret_h`, and the ledger's settled cash must never go negative. It writes nothing.
- **Walk-forward (M4.2):**
  - test quarters have 2 years of calendar before them, and every D0 + 60 has passed (all trades complete);
  - `GRID` is 12 configs on purpose: fixed holds 5/10/20/60, 8%/15% stops, 8% stop + 20% target, 10%/20% trailing, ATR ×2/×3, member sale;
  - training for quarter Q uses signals with D0 in the 2 years before Q that exited under *every* grid rule before Q's first day (no leakage, and every rule is scored on the same trades);
  - the score is the mean over filings of the net excess vs SPY; the best wins, ties go to the earlier (simpler) rule, and fewer than 50 training filings falls back to hold 20 (`params.fallback`);
  - `evaluate()` results are cached per (signal, rule), so a run takes a few seconds.
- **Books** (`exit_backtests.book`), all over the same test signals:
  - `walk_forward`: each quarter traded with its chosen rule, as one continuous T+1 ledger, so cash and positions carry across quarters;
  - `hold_1` … `hold_60`: the baselines at the leaderboard's horizons.
- **Rows:** one per book and quarter plus an `all` row per book; the table is replaced each run.
  - `n_filings`, `mean_ret`, `mean_abn_ret` and `hit_rate` are per filing over **every** test signal, so books differ only by exits. Computing them from the ledger's buys would mix in which signals found cash.
  - `total_return`, `max_drawdown` and `skipped_cash` come from the ledger (5% positions; idle cash earns nothing, so it isn't comparable with a fully invested `spy_return`).

### Alerts, `alerts/`
- **Qualifies:** filings detected live (`available_basis = 'seen'`) first seen at or after the alerts start time, from members on the active watchlist:
  - `watchlist_buy`: a BUY with a symbol, either a stock (or other listed asset) or bought calls (`is_call`); puts are skipped.
  - `held_sale`: a SELL / SELL_PARTIAL of a symbol in an open `my_positions` row.
  - Scanned filings get one heads-up (`filing_alerts`).
- **Score (v2, 0–100), `alerts/score.py`:**
  - expected 20-day excess vs the S&P 500 = the member's shrunk score (`member_horizon_stats`, h = 20; the pooled mean for a member with < 20 filings, as on the leaderboard) + the trade's feature effects (`signal_factors.effect`; features in `common/signals.py`);
  - score = 50 + 1000 × expected, clamped (50 = no edge, 10 points per 1%);
  - the email lists the expected excess, the member part and only the features that move it.
- **Score (v1)**, used until the analytics tables exist: buy 50 / calls 40; amount +0…+20; delay ≤7d +15 … >45d −5; listed stock +15, ETF +5, unlisted −10.
- **Suggested entry:** from `entry_delays` (the member's best delay, else the trade's market-cap bucket's, else all buys'). It's in the email and stored in `alerts.suggested_entry`.
- **Start time:** the first real `alerts.run` stores it in `source_state` (`alerts.start`), so the backlog is never emailed. `--since` overrides it.
- **Suggested exit (passive):** buy emails show the `exit_rules` recommendation once, below the trades ("If you buy: suggested exit is to …"), stored in `alerts.suggested_exit`. It's never an alert by itself.
- **Research (M5.3):** a filing whose buys include a score ≥ 60 gets a "Research" section before the filing link: Claude's news summary plus fact lines (see Agents → researcher).
  - `alerts.run.run(research=)` is injected by `pipeline/run.py` (and `alerts.run main`), so alerts never imports agents.
  - A research exception only drops the section. A brief without a news summary is marked and counted (`research_failed`, a pipeline warning).
- **Exit emails only for positions you logged** (`alerts/positions.py`, run at the end of `alerts.run`):
  - **Watched:** an open `my_positions` row whose `exit_rule` is a rule label; free text isn't watched.
  - **Prices:** stored bars, split-adjusted and not dividend-adjusted (your fill's basis). Your fill is the entry; on the buy day only the close counts.
  - **MemberSale** fires as soon as the source trade's member has a sale filing of the symbol available after the buy.
  - **`data_end`:** fires when the ticker's bars stop 5+ trading days before SPY's. A position whose buy-day close is more than 40% off the buy price (a split) is skipped with a warning.
  - **Once:** one email per position, recorded in `exit_alerts` after sending; never repeated.
- **Delivery:** one email per filing. `alerts` / `filing_alerts` / `exit_alerts` rows are written only after a successful send, so failures retry next run.
- **Settings:** `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (a Google App Password), and optionally `ALERT_RECIPIENT`, all in `.env`.

### Agents, `agents/`
- **Read-only by construction** (`tools.py`). The agent's connection opens the file with `mode=ro` and `query_only`. An authorizer allows only reads, so writes, ATTACH, PRAGMA and DDL fail, even inside a CTE. A progress handler stops queries after 10 s.
  - Tools: `list_tables`, `describe_table` (the table's block from `db/schema.sql` with its comments, plus 3 sample rows) and `query` (one SELECT, ≤ 200 rows, ≤ 40k chars, truncation flagged).
  - SQL errors go back to the model as `is_error` results so it can fix the query.
  - Web search is Anthropic's server tool (`web_search_20260209`); each agent sets `max_uses`.
- **Two backends**, same tools, prompt, output and logging (`runner.run_agent(backend=)`):
  - `claude_code` (default for `agents.ask`): `claude_code.py` runs `claude -p` on the user's Claude subscription. There's no API key and no per-token bill; it counts toward the plan's limits.
    - The DB tools come from `mcp_server.py` over MCP. `--tools` keeps only WebSearch, `--permission-mode dontAsk` denies anything not in `--allowedTools`, and `--setting-sources ""` ignores user settings and hooks. It runs in an empty temp dir (no CLAUDE.md).
    - `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are stripped from its environment, so it never bills the key.
    - The answer comes back via `--json-schema`, and tool calls are parsed from the `stream-json` events as they arrive.
    - `usage.auth` is Claude Code's `apiKeySource` (`none` = subscription login). `usage.est_cost_usd` is its list-price estimate, not a charge.
    - Web search can't be capped per run here (only on or off). A run is killed after 15 min.
    - `--safe-mode` would also drop `--mcp-config` servers, and `--bare` requires an API key, so neither is used.
    - **Under launchd** (no shell PATH), `find_claude()` checks `CLAUDE_BIN`, then PATH, then `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`.
    - The subprocess always gets `USER` and the system PATH (`/usr/bin:/bin:/usr/sbin:/sbin`). Without `USER`, Claude Code can't find its Keychain login ("Not logged in").
  - `api` (`--api`): a manual loop on the Anthropic API with the refusal fallback (`fallbacks="default"`). It's written by hand because the SDK's Python tool runner drops `pause_turn`, and every call must be logged.
- **Model:** `claude-opus-5-5`, effort `high`, on both backends.
  - The final answer is structured output (`proposals.OUTPUT_SCHEMA`): `summary` + `proposals`.
  - The shared system prompt (`BASE_SYSTEM`: project, D0 rules, filing as the unit) is cached. Today's date goes in the user turn so the cached prefix doesn't change.
- **Logging:** the `agent_runs` row is inserted (`status = running`) before the first call. `tools_called` is updated every turn, and the row ends `ok` or `failed`, with `error`, `usage` (tokens, web searches, models) and `output` = the summary.
  - API errors, refusals, `max_tokens`, invalid JSON and `max_turns` (20) all end as `failed`, with no proposals.
- **Proposals** (`agent_proposals`), one row each:
  - `watchlist_add` / `watchlist_remove` need a known `member_id` and evidence (`{claim, source}`, where source is the SQL or a URL).
  - One that can't apply (unknown member, already watched, not watched, no evidence) is downgraded to a `note` with a warning.
  - `note` carries everything else, e.g. exit-rule ideas, since `exit_rules` is rebuilt nightly.
- **Rule-based watchlist review** (`watchlist_review.py`, nightly stage after `leaderboard`; no model, free). It reads the latest `member_scores` snapshot:
  - add: top `TOP_N` (5), `shrunk_score` ≥ `MIN_SCORE` (+0.5%) and `hit_rate` ≥ 50%, not watched;
  - remove: watched and ranked with `shrunk_score` < 0 (unranked members are never proposed);
  - the evidence is the member's leaderboard numbers, with the `member_scores` SQL as the source;
  - no repeats while a proposal for the same (kind, member) is pending or approved-but-unapplied, or for 90 days after a rejection;
  - a run (`agent = watchlist_review`, `model` NULL) is written only when there is something new.
- **Signal researcher** (`researcher.brief`, called by the alerts stage before each high-score buy email):
  - **Threshold:** at least one buy with v2 score ≥ `MIN_SCORE` (60 = an expected 20-day excess of at least +1%); only those buys are researched.
  - **Facts** (`facts()` / `render()`), per symbol:
    - the next and last earnings dates (yfinance `get_earnings_dates`, 15 s cap; a next date within 20 weekdays is flagged as inside the score's horizon);
    - company/sector and `committee_relevant`;
    - the move since the trade date (labeled context only), and the 5- and 20-day moves;
    - the member's earlier trades in the symbol.
    - Per filing: the member's committees in the trade's Congress (`congress_of` is copied from enrich to keep stages decoupled) and leaderboard row.
  - **Narrative:** Claude Code with web search (≤ 5 searches, effort medium, ≤ 10 turns, **180 s timeout**): ≤ ~120 words of material news from the last 30 days, ending with "Sources:" URLs. The prompt warns that search results include old articles.
  - **Fallback:** if it fails, `narrative=None` and the email carries the facts alone.
  - **Reuse:** a successful run for the same `doc_id` (`agent_runs.inputs.doc_id`) in the last 24 h is reused, so a failed send that retries isn't researched again.
  - Runs are logged as `signal_researcher`, with the search result URLs in `tools_called`. Display only: it never feeds the score.
- **Daily digest** (`digest.py`, stage `digest` after `alerts`), shown on the Agents page only (no email):
  - **When:** once per weekday, on the first pass after that day's nightly stages succeeded (`source_state` `pipeline.nightly` day > `agents.digest` day).
    - It's a regular stage, not a nightly one: a failed nightly stage re-runs them all.
    - It runs after `alerts`, so tonight's exit emails are included.
  - **Window:** from the previous digest's end (`agents.digest.changed_at`) to now; the first digest covers 24 h. Monday's covers the weekend.
  - **Facts** (SQL, `facts()`), rendered by code (`render()`):
    - new live-seen filings, by chamber, plus watched members' filings with their trades and alert scores;
    - alerts sent;
    - open positions: last close vs your buy, the S&P 500 over the same span, days held vs max hold, rule status;
    - exits triggered;
    - outcomes: watched or alerted copyable buys reaching D0 + 5/20/60 in the window, one line per filing and symbol, from D0 only;
    - leaderboard: top-10 entries and exits, watched members' ranks;
    - pending proposals.
  - **Narrative:** Claude (Claude Code backend, effort medium, ≤ 8 turns, no web search) gets the facts as JSON and writes ≤ ~150 words on top. The code-rendered sections always follow, so numbers are exact even if the model is wrong.
  - **Fallback:** if the narrative fails, the failed run stays logged and a second `daily_digest` run (`model` NULL) holds "(Written from the template: …)" + the sections; this is a pipeline warning. The app's "Mark read" sets `agent_runs.approved = 1`.
- **Apply** (`apply.py`, pipeline stage `apply_proposals`, before `alerts`):
  - approved, unapplied proposals: add = the app's watch upsert (`reason = 'agent run N: title'`), remove = `active = 0`, note = `acknowledged`;
  - `applied_at` is set even on failure (`apply_result = 'failed: …'`, a pipeline warning), so nothing retries forever.
  - Like a manual add, an added member's live filings since the alerts start become eligible for alerts.
- **Credentials:** none for the default Claude Code backend (the `claude` CLI must be installed and logged in). `--api` needs `ANTHROPIC_API_KEY` in `.env`.

### Scheduling, `pipeline/`
- **One run** calls each stage's public `run()` in order: ingest House, ingest Senate, parse, enrich, apply approved agent proposals, alerts, digest (when due).
  - **Nightly stages** (`prices`, `outcomes`, `open_inflation`, `leaderboard`, `watchlist_review`, `factors`, `exits`, `securities`) also run in the first run at or after 18:00 ET on a weekday with no successful nightly for that day (`source_state` `pipeline.nightly`), so a missed night catches up on wake. `--nightly` forces them.
  - Every stage runs even if an earlier one failed.
  - The run is recorded in `pipeline_runs` (stage summaries and errors as JSON).
  - A file lock (`~/TradeTracker/pipeline.lock`) allows one run at a time.
- **Failure vs warning:**
  - Failure: an exception, an ingest `failed_sources`, missing Gmail settings, or an alert that failed to send.
  - Warning: downloads that will retry, newly failed or needs-review parses, unmatched filers.
- **Due rule** (politeness lives in code): a run is skipped unless the last one started at least 29 min ago (weekdays) or 119 min ago (weekends, America/New_York). `--force` skips the check.
- **Failures are shown, not emailed.** The app's Pipeline tab (`/api/pipeline/health`, `/api/pipeline/runs`) shows the last run, failures in a row, a stale warning, and recent runs with their errors and warnings. `/api/pipeline/history` adds price coverage, filings by year (the review queue) and the last nightly run. Gmail is for trade alerts only; don't add pipeline-health emails.
- **launchd:** `~/Library/LaunchAgents/com.tracker.pipeline.plist` fires every 30 min plus once at load, with output in `logs/launchd.log`.
  - Nothing runs while the Mac sleeps; the next run catches up.
  - Re-run `python -m pipeline.schedule install` after moving the repo or recreating `.venv` (the plist stores both paths).

## Definitions (use these exactly)

- **D0**: the first trading-day open after the filing became available to us (`filings.available_at`). This is the earliest realistic entry.
  - `available_basis = 'seen'`: detected live, so `available_at = first_seen_at`.
  - `available_basis = 'filed'`: backfilled (or loaded before scheduling started on 2026-10-01), so we never saw it go public; `available_at` = after the close on `filing_date` (`T21:00:00Z`), i.e. D0 = the next trading day. Backfilled filings are never alerted.
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
- `filings.parse_status`: `pending | parsed | needs_review | failed`
- `my_positions.status`: `open | closed`
- `filings.available_basis`: `seen | filed`
- `trades.mcap_bucket`: `mega | large | mid | small | micro` (≥$200B, $10B, $2B, $300M)
- `price_coverage.status`: `ok | partial | missing`
- `agent_runs.approved`, `agent_proposals.approved`: `NULL` (pending) | `1` (approved) | `0` (rejected)
- `agent_runs.status`: `running | ok | failed`
- `agent_proposals.kind`: `watchlist_add | watchlist_remove | note`
- Amount ranges are stored as integer `amount_min` / `amount_max`.
- Low-quality parses carry a `confidence` value.

## Conventions

- **Parsers need fixtures.** Every parser change comes with a saved sample filing in `tests/fixtures/` and a test. Source formats change without warning, and the fixtures catch it.
- **Idempotent jobs.** Every job must be safe to re-run. Upsert by natural keys (`doc_id`, `(ticker, date)`, etc.).
- **Backtests report out-of-sample results only.** Walk-forward: train on a rolling 2 years, test on the next quarter. Include costs, slippage, and the Roth cash-settlement constraint. Keep parameter grids small.
- **Leaderboard.** Use shrinkage (empirical Bayes) scores and a minimum sample size, so a member with 3 lucky trades can't top the list.
- **Survivorship bias.** Delisted tickers are missing from free price data. Flag any conclusions drawn before the switch to a paid provider.
- **Agent auditing.** Log every agent run (prompt, tools called, output, approval) to `agent_runs`.
- Log to file, and alert by email on repeated pipeline failures.
- Keep modules small and plain. Prefer simple functions over frameworks.
- **Schema changes** go in `db/schema.sql` *and* a new `db/migrations/NNN_*.sql` (see Database ownership). Then rebuild the app so it picks up both.
- **New app page:** add a repository method, then a controller endpoint under `/api`, then `src/pages/X.tsx` using `useApi` + `Table`, then a route and nav entry in `App.tsx`. Pages must show a helpful empty state when their pipeline hasn't produced data yet.
- **App tests.**
  - Backend: `app/src/test/java/com/tracker/ApiTest.java` (`@SpringBootTest` + MockMvc on a temp SQLite file, seeded in `@BeforeEach`). Every new endpoint gets a test there.
  - Frontend: `app/frontend/src/*.test.ts(x)` with Vitest + Testing Library. `src/test/mockApi.ts` stubs `fetch` (`mockApi({...EMPTY_DB, 'GET /api/x': rows})`). Every new page gets an empty-state smoke test.
  - Maven runs the frontend tests on its own Node (v22, pinned in `pom.xml`), which can differ from your local Node. Check `mvn test`, not just `npm test`.
- **DB constraint errors** (unknown member/trade id) return 400 via `api/ApiErrors.java`. SQLite errors reach Spring uncategorized, so that class checks for SQLite's constraint error code itself.
- **Verify app changes** by running `mvn package` (tests included), then starting the jar against a throwaway `TRACKER_DB_PATH` and checking the endpoints with `curl`.
