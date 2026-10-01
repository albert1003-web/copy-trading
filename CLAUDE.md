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

- **App window** (`Browser.java`). The app launches Chrome (or Edge/Brave/Chromium) in `--app` mode with its own profile in `~/TradeTracker/window-profile`, so there are no tabs or address bar. Closing the window quits the server, and quitting the server closes the window. Without a Chromium browser it falls back to a tab in the default browser.
- **Single instance.** If port 8787 is already taken, launching the app only opens a window.
- **Quit.** Close the window, or use the UI's Quit button (`POST /api/shutdown`).
- **Packaging.** `package-mac.sh` adds the icon (`packaging/TradeTracker.icns`, drawn by `packaging/make-icon.swift`; delete the `.icns` to regenerate it). It sets `LSUIElement` so the background server stays out of the Dock, then re-signs ad hoc.
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
ingest/      house.py, senate.py (planned)
parse/       house_pdf.py, senate_html.py, llm_fallback.py (planned)
enrich/      tickers.py, committees.py                   (planned)
prices/      fetch.py                                    (planned)
analytics/   outcomes.py, open_inflation.py, exits.py, leaderboard.py (planned)
alerts/      score.py, email.py, positions.py            (planned)
agents/      tools.py, digest.py, researcher.py, strategist.py (planned)
tests/       conftest.py (temp DB, FakeHouseClerk via httpx.MockTransport), test_db.py, test_house_ingest.py, fixtures/
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
