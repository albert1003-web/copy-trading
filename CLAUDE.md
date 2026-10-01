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
- **The app writes only user-owned data:** `watchlist`, `my_positions`, and `agent_runs.approved`. Every other table is read-only from the app.
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
  frontend/src/              App.tsx (nav/routes), pages/, api.ts, hooks.ts, components.tsx, format.ts
common/      config.py (paths/env), http.py (polite client: UA, retries, pauses), log.py
db/          __init__.py (connect + migrations), init.py, schema.sql, migrations/
ingest/      house.py, senate.py
parse/       normalize.py (enums, amounts, tickers), house_pdf.py, senate_html.py, run.py; llm_fallback.py (planned)
enrich/      reference.py (cached legislators + symbol lists), members.py, tickers.py, run.py,
             member_aliases.csv, ticker_aliases.csv; committees.py (planned)
prices/      fetch.py                                    (planned)
analytics/   outcomes.py, open_inflation.py, exits.py, leaderboard.py (planned)
alerts/      score.py (v1 score), rules.py (what qualifies), email.py (compose + Gmail), run.py;
             positions.py (planned)
agents/      tools.py, digest.py, researcher.py, strategist.py (planned)
tests/       conftest.py (temp DB, FakeHouseClerk / FakeSenateEfd via httpx.MockTransport), test_db.py,
             test_house_ingest.py, test_senate_ingest.py, test_normalize.py, test_parse_fixtures.py,
             test_parse_run.py, fixtures/ (house/electronic_*.pdf + senate/ptr_*.html, each with .expected.json)
.github/workflows/  poll.yml (30 min), nightly.yml, weekly.yml (planned)
```

## Stack

- **Pipelines:** Python 3.12
- **Scraping:** httpx/requests, pdfplumber, BeautifulSoup
- **Storage:** SQLite (phase 1), then Postgres on Supabase/Neon. Write portable SQL and avoid SQLite-only features.
- **Analytics:** pandas + DuckDB (vectorbt optional for exit grids)
- **Prices:** yfinance to start, then Alpaca/Polygon/Tiingo before trusting results
- **Scheduling:** local cron, then GitHub Actions cron
- **Alerts:** Gmail SMTP with an app password
- **Agents:** Anthropic Python SDK with tool use. Default to the latest capable Claude model.
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
python -m alerts.run --dry-run --since 2026-09-01   # print what would be sent; writes nothing

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
  - `member_aliases.csv` overrides both. Unmatched filers are logged and make the run exit 1.
- **Tickers:** `trades.ticker` stays as parsed. Enrichment writes `symbol`, `ticker_status`, and `is_etf`:
  - `listed`: on the lists, with `BRK-B` / `BRK/B` normalized to `BRK.B`.
  - `renamed`: resolved through `ticker_aliases.csv`; add a row only after confirming the new symbol is listed.
  - `unlisted`: OTC or delisted; `symbol` = the ticker as filed.
  - `none`: no ticker; `symbol` is NULL.
- Everything is recomputed on each run, so a `parse.run --reparse` is fixed up by the next `enrich.run`.

### Alerts, `alerts/`
- **Qualifies:** filings first seen at or after the alerts start time, from members on the active watchlist:
  - `watchlist_buy`: a BUY with a symbol, either a stock (or other listed asset) or bought calls (`is_call`); puts are skipped.
  - `held_sale`: a SELL / SELL_PARTIAL of a symbol in an open `my_positions` row.
  - Scanned filings get one heads-up (`filing_alerts`).
- **Score (v1, 0–100):** buy 50 / calls 40; amount +0…+20; delay ≤7d +15 … >45d −5; listed stock +15, ETF +5, unlisted −10. The reasons are shown in the email.
- **Start time:** the first real `alerts.run` stores it in `source_state` (`alerts.start`), so the backlog is never emailed. `--since` overrides it.
- **Delivery:** one email per filing. `alerts` / `filing_alerts` rows are written only after a successful send, so failures retry next run.
- **Settings:** `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (a Google App Password), and optionally `ALERT_RECIPIENT`, all in `.env`.

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
- `filings.parse_status`: `pending | parsed | needs_review | failed`
- `my_positions.status`: `open | closed`
- `agent_runs.approved`: `NULL` (pending) | `1` (approved) | `0` (rejected)
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
