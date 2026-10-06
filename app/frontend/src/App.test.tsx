import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { EMPTY_DB, mockApi } from './test/mockApi'

// Let in-flight fetches settle inside act() before Testing Library unmounts (avoids act() warnings).
afterEach(async () => {
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)))
})

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )
}

describe('smoke: every page renders on an empty database', () => {
  const pages: [string, string, RegExp][] = [
    ['/', 'Dashboard', /No alerts yet/],
    ['/pipeline', 'Pipeline', /No runs yet/],
    ['/trades', 'Trades', /No trades match/],
    ['/watchlist', 'Watchlist', /Nobody on the watchlist yet/],
    ['/leaderboard', 'Leaderboard', /No scores yet/],
    ['/outcomes', 'Outcomes', /No outcomes yet/],
    ['/open-inflation', 'Open inflation', /No open-inflation results yet/],
    ['/positions', 'Positions', /No positions logged yet/],
    ['/agents', 'Agents', /No agent runs yet/],
  ]

  it.each(pages)('%s shows the %s page with an empty state', async (path, heading, empty) => {
    mockApi(EMPTY_DB)
    renderAt(path)
    expect(screen.getByRole('heading', { level: 1, name: heading })).toBeInTheDocument()
    expect(await screen.findByText(empty)).toBeInTheDocument()
    expect(screen.queryByText(/failed/)).not.toBeInTheDocument()
  })

  it('shows all nav links', async () => {
    mockApi(EMPTY_DB)
    renderAt('/')
    await screen.findByText(/No filings yet/)
    const nav = screen.getByRole('navigation')
    for (const label of ['Dashboard', 'Trades', 'Watchlist', 'Leaderboard', 'Outcomes', 'Open inflation', 'Positions', 'Agents',
      'Pipeline']) {
      expect(within(nav).getByRole('link', { name: label })).toBeInTheDocument()
    }
  })

  it('shows a not-found message for unknown routes', () => {
    mockApi(EMPTY_DB)
    renderAt('/nope')
    expect(screen.getByText('Page not found.')).toBeInTheDocument()
  })
})

describe('pages with data', () => {
  it('dashboard shows summary counts', async () => {
    mockApi({ ...EMPTY_DB, 'GET /api/summary': { ...EMPTY_DB['GET /api/summary'], trades: 42, alerts: 7 } })
    renderAt('/')
    expect(await screen.findByText('42')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
  })

  it('dashboard shows recent filings with district and scanned tag', async () => {
    mockApi({
      ...EMPTY_DB,
      'GET /api/filings/recent': [{
        doc_id: '9116342', member_name: 'Hon. Harold Dallas Rogers', chamber: 'house', state_district: 'KY05',
        filing_date: '2026-09-23', first_seen_at: '2026-10-01T13:00:00Z', doc_format: 'scanned',
        parse_status: 'needs_review', source_url: 'https://example.com/9116342.pdf',
      }],
    })
    renderAt('/')
    expect(await screen.findByText('Hon. Harold Dallas Rogers')).toBeInTheDocument()
    expect(screen.getByText('KY05')).toBeInTheDocument()
    expect(screen.getByText('2026-09-23')).toBeInTheDocument()
    expect(screen.getByText('scanned')).toBeInTheDocument()
    expect(screen.getByText('needs review')).toBeInTheDocument()
  })

  it('trades table renders a row and sends filters to the API', async () => {
    const fetchMock = mockApi({
      ...EMPTY_DB,
      'GET /api/trades': [{
        trade_id: 1, ticker: 'NVDA', asset_name: 'NVIDIA Corporation - Common Stock (NVDA)', action: 'BUY', owner: 'spouse', member_name: 'Nancy Pelosi', party: 'D',
        tx_date: '2026-09-10', disclosure_date: '2026-09-28', amount_min: 1000001, amount_max: 5000000,
        filing_delay_days: 18, score: 64, source_url: 'https://example.com/f.pdf',
      }],
    })
    renderAt('/trades')
    expect(await screen.findByText('NVDA')).toBeInTheDocument()
    expect(screen.getByText('Nancy Pelosi')).toBeInTheDocument()
    expect(screen.getByText('NVIDIA Corporation - Common Stock (NVDA)')).toBeInTheDocument()
    expect(screen.getByText('$1M–$5M')).toBeInTheDocument()
    expect(screen.getByText('64')).toBeInTheDocument()

    await userEvent.type(screen.getByPlaceholderText('Ticker'), 'aapl')
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([u]) => String(u))
      expect(urls.some((u) => u.startsWith('/api/trades?') && u.includes('ticker=aapl'))).toBe(true)
    })
  })

  it('trades table shows renamed, unlisted and delisted tickers', async () => {
    mockApi({
      ...EMPTY_DB,
      'GET /api/trades': [
        { trade_id: 1, ticker: 'SQ', symbol: 'XYZ', ticker_status: 'renamed', action: 'BUY' },
        { trade_id: 2, ticker: 'TGOPY', symbol: 'TGOPY', ticker_status: 'unlisted', action: 'BUY' },
        { trade_id: 3, ticker: 'GOGL', symbol: null, ticker_status: 'delisted', action: 'BUY' },
      ],
    })
    renderAt('/trades')
    expect(await screen.findByText('XYZ')).toBeInTheDocument()
    expect(screen.getByText('(filed as SQ)')).toBeInTheDocument()
    expect(screen.getByText('unlisted')).toBeInTheDocument()
    expect(screen.getByText('GOGL')).toBeInTheDocument()
    expect(screen.getByText('delisted')).toBeInTheDocument()
  })

  it('trades table tags trades Claude read from a scanned filing', async () => {
    mockApi({
      ...EMPTY_DB,
      'GET /api/trades': [{ trade_id: 1, ticker: 'GDX', symbol: 'GDX', ticker_status: 'listed', action: 'BUY', parse_method: 'vision' }],
    })
    renderAt('/trades')
    expect(await screen.findByText('read by Claude')).toHaveAttribute('title', expect.stringContaining('Not used in analytics'))
  })

  it('trades table shows sector, committee relevance and estimated disclosure', async () => {
    mockApi({
      ...EMPTY_DB,
      'GET /api/trades': [
        { trade_id: 1, ticker: 'LMT', symbol: 'LMT', ticker_status: 'listed', action: 'BUY', disclosure_date: '2020-03-02',
          sector: 'Industrials', industry: 'Aerospace & Defense', mcap_bucket: 'large', committee_relevant: 1, available_basis: 'filed' },
      ],
    })
    renderAt('/trades')
    expect(await screen.findByText('Industrials')).toBeInTheDocument()
    expect(screen.getByText('committee')).toBeInTheDocument()
    expect(screen.getByText('large')).toBeInTheDocument()
    expect(screen.getByText(/est\./)).toBeInTheDocument()
  })

  it('pipeline page shows when the pipeline has never run', async () => {
    mockApi(EMPTY_DB)
    renderAt('/pipeline')
    expect(await screen.findByText(/pipeline hasn't run yet/)).toBeInTheDocument()
    expect(await screen.findByText(/Prices haven't been fetched yet/)).toBeInTheDocument()
    expect(screen.getByText(/No filings yet. Backfill history/)).toBeInTheDocument()
  })

  it('pipeline page shows price coverage and filings by year', async () => {
    mockApi({
      ...EMPTY_DB,
      'GET /api/pipeline/history': {
        price_coverage: { ok: 90, partial: 6, missing: 4 }, trades_with_symbol: 200, trades_priced: 150,
        review_queue: [{ chamber: 'senate', year: 2020, filings: 179, parsed: 146, scanned: 33, needs_review: 0, failed: 2, pending: 0 }],
        last_nightly: '2026-10-01',
      },
    })
    renderAt('/pipeline')
    expect(await screen.findByText(/150 of 200 trades fully priced \(75%\)/)).toBeInTheDocument()
    expect(screen.getByText('4 missing')).toBeInTheDocument()
    expect(screen.getByText(/Last nightly run: 2026-10-01/)).toBeInTheDocument()
    expect(screen.getByText('179')).toBeInTheDocument()
  })

  it('pipeline page shows a failing, stale pipeline and its runs', async () => {
    const threeHoursAgo = new Date(Date.now() - 3 * 3600_000).toISOString()
    mockApi({
      ...EMPTY_DB,
      'GET /api/pipeline/health': {
        last_run: {
          run_id: 9, started_at: threeHoursAgo, status: 'failed', warnings: '["parse: 1 new filing(s) need review"]',
          stages: JSON.stringify({ ingest_house: { ok: true }, ingest_senate: { ok: false } }),
        },
        last_ok_at: null, failure_streak: 3, runs_24h: 5,
      },
      'GET /api/pipeline/runs': [{
        run_id: 9, started_at: '2026-10-01T18:00:00Z', status: 'failed', warnings: '[]',
        stages: JSON.stringify({
          ingest_house: { ok: true, errors: [], summary: { new_from_index: 2, new_from_search: 0 } },
          ingest_senate: { ok: false, errors: ['senate search failed'], summary: { new: 0 } },
          parse: { ok: true, errors: [], summary: { rows: 7 } },
        }),
      }],
    })
    renderAt('/pipeline')
    expect(await screen.findByText('Failing: ingest senate (3 runs in a row)')).toBeInTheDocument()
    expect(screen.getByText(/Last run 3 h ago · failed/)).toBeInTheDocument()
    expect(screen.getByText(/No runs for 3 h/)).toBeInTheDocument()
    expect(screen.getByText(/1 warning: parse/)).toBeInTheDocument()
    expect(await screen.findByText('2 new filings · 7 trades parsed')).toBeInTheDocument()
    expect(screen.getByText('senate search failed')).toBeInTheDocument()
  })

  it('watchlist adds a member', async () => {
    const posted = vi.fn()
    mockApi({
      ...EMPTY_DB,
      'GET /api/members': [{ member_id: 'T000278', name: 'Tommy Tuberville', party: 'R', state: 'AL', chamber: 'senate', watched: 0 }],
      'POST /api/watchlist': (body: unknown) => { posted(body) },
    })
    renderAt('/watchlist')
    const select = await screen.findByRole('combobox')
    await screen.findByRole('option', { name: /Tommy Tuberville/ })
    await userEvent.selectOptions(select, 'T000278')
    await userEvent.type(screen.getByPlaceholderText('Reason (optional)'), 'committee')
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(posted).toHaveBeenCalledWith({ memberId: 'T000278', reason: 'committee' }))
  })

  it('positions logs a buy', async () => {
    const posted = vi.fn()
    mockApi({ ...EMPTY_DB, 'POST /api/positions': (body: unknown) => { posted(body) } })
    renderAt('/positions')
    await userEvent.type(screen.getByPlaceholderText('Ticker'), 'NVDA')
    await userEvent.type(screen.getByPlaceholderText('Buy price'), '180.5')
    await userEvent.type(screen.getByPlaceholderText('Shares'), '10')
    await userEvent.click(screen.getByRole('button', { name: 'Log buy' }))
    await waitFor(() => expect(posted).toHaveBeenCalledWith(expect.objectContaining({
      ticker: 'NVDA', buyPrice: 180.5, shares: 10, exitRule: null, tradeId: null,
    })))
  })

  it('positions default the exit rule to the recommendation and show triggered exits', async () => {
    const posted = vi.fn()
    mockApi({
      ...EMPTY_DB,
      'GET /api/exit-rules': [
        { label: 'trailing_stop(pct=0.1)', description: 'Trailing stop 10%', recommended: 0, confidence: null, reason: null },
        { label: 'fixed_hold(days=5)', description: 'Hold 5 trading days', recommended: 1, confidence: 'low', reason: 'beat 2 of 5' },
      ],
      'GET /api/positions': [
        { position_id: 1, ticker: 'NVDA', status: 'open', buy_date: '2026-09-01', buy_price: 180, shares: 10,
          exit_rule: 'trailing_stop(pct=0.1)', triggered_on: '2026-09-15', exit_reason: 'stop' },
        { position_id: 2, ticker: 'AAPL', status: 'open', buy_date: '2026-09-02', buy_price: 220, shares: 5,
          exit_rule: 'fixed_hold(days=5)', triggered_on: null, last_close: 231, last_date: '2026-09-04',
          market_value: 1155, pnl: 55, ret: 0.05, spy_ret: 0.01, excess: 0.04, days_held: 2, max_hold: 5,
          stop_level: null, target_level: null },
      ],
      'POST /api/positions': (body: unknown) => { posted(body) },
    })
    renderAt('/positions')
    const select = await screen.findByLabelText('Exit rule') as HTMLSelectElement
    await waitFor(() => expect(select.value).toBe('fixed_hold(days=5)'))
    expect(screen.getByText(/Recommended exit:/)).toBeInTheDocument()
    expect(screen.getByText(/^triggered/)).toBeInTheDocument()
    expect(screen.getByText('watching')).toBeInTheDocument()
    expect(screen.getByText('day 2 of 5')).toBeInTheDocument()
    expect(screen.getByText('$231.00')).toBeInTheDocument()
    expect(screen.getByText('Unrealized P&L')).toBeInTheDocument()
    expect(screen.getByText('+4.0%')).toBeInTheDocument()

    await userEvent.type(screen.getByPlaceholderText('Ticker'), 'MSFT')
    await userEvent.type(screen.getByPlaceholderText('Buy price'), '400')
    await userEvent.type(screen.getByPlaceholderText('Shares'), '2')
    await userEvent.click(screen.getByRole('button', { name: 'Log buy' }))
    await waitFor(() => expect(posted).toHaveBeenCalledWith(expect.objectContaining({ exitRule: 'fixed_hold(days=5)' })))

    await userEvent.type(screen.getByPlaceholderText('Ticker'), 'AMD')
    await userEvent.type(screen.getByPlaceholderText('Buy price'), '150')
    await userEvent.type(screen.getByPlaceholderText('Shares'), '3')
    await userEvent.selectOptions(screen.getByLabelText('Exit rule'), "Don't watch")
    await userEvent.click(screen.getByRole('button', { name: 'Log buy' }))
    await waitFor(() => expect(posted).toHaveBeenLastCalledWith(expect.objectContaining({ ticker: 'AMD', exitRule: null })))
  })

  it('agents marks a digest as read', async () => {
    const decided = vi.fn()
    mockApi({
      ...EMPTY_DB,
      'GET /api/agent-runs': [{ run_id: 3, agent: 'daily_digest', started_at: '2026-09-29T20:00:00Z', status: 'ok', output: 'Quiet day.', approved: null }],
      'POST /api/agent-runs/3/decision': (body: unknown) => { decided(body) },
    })
    renderAt('/agents')
    expect(await screen.findByText('Quiet day.')).toBeInTheDocument()
    expect(screen.getByText('New')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Mark read' }))
    await waitFor(() => expect(decided).toHaveBeenCalledWith({ approved: true }))
  })

  it('agents decides each proposal on its own', async () => {
    const decided = vi.fn()
    mockApi({
      ...EMPTY_DB,
      'GET /api/agent-runs': [{
        run_id: 4, agent: 'researcher', started_at: '2026-10-05T14:00:00Z', status: 'ok', output: 'Pelosi looks strong.', approved: null,
        proposals: [
          { proposal_id: 20, kind: 'watchlist_add', member_id: 'P000197', member_name: 'Nancy Pelosi', title: 'Add Pelosi',
            rationale: 'Ranked 1st.', evidence: [{ claim: '20-day excess +2%', source: 'SELECT 1' }], approved: null },
          { proposal_id: 21, kind: 'note', member_id: null, title: 'Try a 10-day hold', rationale: 'Maybe.', evidence: [],
            approved: 1, applied_at: '2026-10-05T15:00:00Z', apply_result: 'acknowledged' },
        ],
      }],
      'POST /api/agent-proposals/20/decision': (body: unknown) => { decided(body) },
    })
    renderAt('/agents')
    expect(await screen.findByText('Add Pelosi')).toBeInTheDocument()
    expect(screen.getByText('Nancy Pelosi')).toBeInTheDocument()
    expect(screen.getByText(/Acknowledged/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Approve/ })).toHaveLength(1)  // no run-level buttons
    await userEvent.click(screen.getByRole('button', { name: 'Reject: Add Pelosi' }))
    await waitFor(() => expect(decided).toHaveBeenCalledWith({ approved: false }))
  })
})

describe('app behavior', () => {
  it('shows an error banner when the API fails', async () => {
    mockApi({ ...EMPTY_DB, 'GET /api/leaderboard': Error })
    renderAt('/leaderboard')
    expect(await screen.findByText(/GET \/leaderboard\?horizon=20 failed: 500/)).toBeInTheDocument()
  })

  it('quit calls shutdown and shows the stopped screen', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const fetchMock = mockApi({ ...EMPTY_DB, 'POST /api/shutdown': { status: 'stopping' } })
    renderAt('/')
    await userEvent.click(screen.getByRole('button', { name: 'Quit' }))
    expect(await screen.findByText('Trade Tracker has stopped')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith('/api/shutdown', expect.objectContaining({ method: 'POST' }))
  })
})
