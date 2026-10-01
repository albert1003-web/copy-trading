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
    ['/trades', 'Trades', /No trades match/],
    ['/watchlist', 'Watchlist', /Nobody on the watchlist yet/],
    ['/leaderboard', 'Leaderboard', /No scores yet/],
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
    for (const label of ['Dashboard', 'Trades', 'Watchlist', 'Leaderboard', 'Positions', 'Agents']) {
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
        filing_delay_days: 18, score: 0.82, source_url: 'https://example.com/f.pdf',
      }],
    })
    renderAt('/trades')
    expect(await screen.findByText('NVDA')).toBeInTheDocument()
    expect(screen.getByText('Nancy Pelosi')).toBeInTheDocument()
    expect(screen.getByText('NVIDIA Corporation - Common Stock (NVDA)')).toBeInTheDocument()
    expect(screen.getByText('$1M–$5M')).toBeInTheDocument()
    expect(screen.getByText('0.82')).toBeInTheDocument()

    await userEvent.type(screen.getByPlaceholderText('Ticker'), 'aapl')
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([u]) => String(u))
      expect(urls.some((u) => u.startsWith('/api/trades?') && u.includes('ticker=aapl'))).toBe(true)
    })
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

  it('agents approves a pending proposal', async () => {
    const decided = vi.fn()
    mockApi({
      ...EMPTY_DB,
      'GET /api/agent-runs': [{ run_id: 3, agent: 'strategy_analyst', started_at: '2026-09-29T20:00:00Z', output: 'Add Tuberville.', approved: null }],
      'POST /api/agent-runs/3/decision': (body: unknown) => { decided(body) },
    })
    renderAt('/agents')
    expect(await screen.findByText('Add Tuberville.')).toBeInTheDocument()
    expect(screen.getByText('Pending')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(decided).toHaveBeenCalledWith({ approved: true }))
  })
})

describe('app behavior', () => {
  it('shows an error banner when the API fails', async () => {
    mockApi({ ...EMPTY_DB, 'GET /api/leaderboard': Error })
    renderAt('/leaderboard')
    expect(await screen.findByText(/GET \/leaderboard failed: 500/)).toBeInTheDocument()
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
