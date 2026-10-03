import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'
import App from './App'
import { ticks } from './charts'
import { EMPTY_DB, mockApi } from './test/mockApi'

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

const BOARD = [
  { member_id: 'P000197', name: 'Nancy Pelosi', party: 'D', chamber: 'house', as_of: '2026-10-03', horizon: 20, rank: 1,
    n_filings: 27, n_trades: 85, mean_ret: 0.0321, mean_spy_ret: 0.0123, mean_abn_ret: 0.0198, hit_rate: 0.56,
    consistency: 0.5, shrunk_score: 0.0131 },
  { member_id: 'X1', name: 'Small Sample', party: 'R', chamber: 'house', as_of: '2026-10-03', horizon: 20, rank: null,
    n_filings: 3, n_trades: 5, mean_ret: 0.2, mean_spy_ret: 0.02, mean_abn_ret: 0.18, hit_rate: 1, consistency: null,
    shrunk_score: 0.05 },
]

describe('leaderboard', () => {
  it('shows avg return next to the S&P 500 and switches horizon', async () => {
    const horizons: (string | null)[] = []
    mockApi({
      ...EMPTY_DB,
      'GET /api/leaderboard': (_b: unknown, url: URL) => { horizons.push(url.searchParams.get('horizon')); return BOARD },
    })
    renderAt('/leaderboard')
    expect(await screen.findByText('Nancy Pelosi')).toBeInTheDocument()
    expect(screen.getByText('+3.2%')).toBeInTheDocument()  // avg return
    expect(screen.getByText('+1.2%')).toBeInTheDocument()  // S&P 500
    expect(screen.getByRole('columnheader', { name: 'Good years' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Nancy Pelosi' })).toHaveAttribute('href', '/outcomes?member=P000197')
    await userEvent.click(screen.getByRole('button', { name: '60d' }))
    expect(horizons).toContain('60')
    expect(screen.queryByRole('columnheader', { name: 'Good years' })).not.toBeInTheDocument()
  })
})

describe('outcomes', () => {
  const SUMMARY = [
    { horizon: 1, n_filings: 1992, n_trades: 15709, mean_ret: 0.002, mean_spy_ret: 0.001, mean_abn_ret: 0.001, hit_rate: 0.5 },
    { horizon: 20, n_filings: 1976, n_trades: 15575, mean_ret: 0.0164, mean_spy_ret: 0.0141, mean_abn_ret: 0.0023, hit_rate: 0.47 },
  ]
  const TRADES = [
    { trade_id: 7, d0_date: '2026-09-08', member_name: 'Nancy Pelosi', symbol: 'NVDA', ret_5: -0.0888, ret_20: 0.02,
      ret_60: null, abn_ret_20: 0.01, win_20: 1, tx_abn_ret: 0.15 },
  ]

  it('charts buys vs the S&P 500 per horizon and lists trades', async () => {
    const members: string[] = []
    mockApi({
      ...EMPTY_DB,
      'GET /api/outcomes/summary': (_b: unknown, url: URL) => { members.push(url.searchParams.get('member') ?? ''); return SUMMARY },
      'GET /api/outcomes/members': [{ member_id: 'P000197', name: 'Nancy Pelosi', n_filings: 27 }],
      'GET /api/outcomes/trades': TRADES,
    })
    renderAt('/outcomes?member=P000197')
    expect(await screen.findByRole('img', { name: /average return of buys and of the S&P 500/ })).toBeInTheDocument()
    expect(screen.getByText('Buys')).toBeInTheDocument()  // legend
    expect(screen.getByText('20 days')).toBeInTheDocument()
    expect(screen.getByText('+1.6%')).toBeInTheDocument()
    expect(screen.getByText('47%')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'NVDA' })).toHaveAttribute('href', '/trades/7')
    expect(members).toContain('P000197')
  })

  it('shows a tooltip for a focused column', async () => {
    mockApi({ ...EMPTY_DB, 'GET /api/outcomes/summary': SUMMARY })
    renderAt('/outcomes')
    const chart = await screen.findByRole('img', { name: /average return/ })
    const band = within(chart).getByLabelText(/^20d:/)
    act(() => band.focus())
    const tip = await screen.findByRole('status')
    expect(within(tip).getByText('S&P 500')).toBeInTheDocument()
    expect(within(tip).getByText('1.4%')).toBeInTheDocument()
  })
})

describe('open inflation', () => {
  it('shows the all-buys chart and best entry per group', async () => {
    const stat = (group_type: string, group_key: string, k: number, mean: number) =>
      ({ group_type, group_key, k, n: 1992, n_trades: 15709, mean, median: 0, share_pos: 0.46, shrunk_mean: mean })
    mockApi({
      ...EMPTY_DB,
      'GET /api/open-inflation': {
        stats: [1, 2, 3, 5].flatMap((k) => [stat('all', 'all', k, -0.002), stat('member', 'P000197', k, k === 3 ? 0.005 : -0.001)])
          .map((s) => (s.group_type === 'member' ? { ...s, name: 'Nancy Pelosi', n: 27 } : s)),
        delays: [
          { group_type: 'all', group_key: 'all', n: 1992, n_trades: 15709, best_k: 0, gain: 0 },
          { group_type: 'member', group_key: 'P000197', n: 27, n_trades: 85, best_k: 3, gain: 0.004, name: 'Nancy Pelosi' },
        ],
      },
    })
    renderAt('/open-inflation')
    expect(await screen.findByRole('img', { name: /Average open inflation/ })).toBeInTheDocument()
    expect(screen.getByText(/Best entry for all buys: the D0 open/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Nancy Pelosi' })).toBeInTheDocument()
    expect(screen.getByText('3 days')).toBeInTheDocument()
  })
})

describe('trade detail', () => {
  const TRADE = {
    trade_id: 7, doc_id: '20035528', symbol: 'NVDA', ticker: 'NVDA', action: 'BUY', asset_name: 'NVIDIA Corp (NVDA)',
    member_name: 'Nancy Pelosi', party: 'D', owner: 'spouse', amount_min: 1000001, amount_max: 5000000,
    tx_date: '2026-09-02', disclosure_date: '2026-09-04', filing_delay_days: 2, filing_date: '2026-09-04',
    available_basis: 'filed', chamber: 'house', source_url: 'https://example.com/f.pdf', d0_date: '2026-09-08',
    d0_open: 233.11, copyable: 1, complete: 0, ret_5: -0.0888, abn_ret_5: -0.0736, win_5: 0, tx_ret: 0.149,
    tx_abn_ret: 0.12, open_infl_1: 0.01,
  }
  const PRICES = {
    symbol: 'NVDA', tx_date: '2026-09-02', d0_date: '2026-09-08',
    bars: [
      { date: '2026-09-02', price: 200, spy: 600, price_adj_open: 199, spy_adj_open: 599 },
      { date: '2026-09-08', price: 236, spy: 605, price_adj_open: 233.11, spy_adj_open: 603 },
      { date: '2026-09-09', price: 220, spy: 600, price_adj_open: 230, spy_adj_open: 604 },
    ],
  }

  it('shows the filing, outcome and price chart vs SPY', async () => {
    mockApi({ ...EMPTY_DB, 'GET /api/trades/7': TRADE, 'GET /api/trades/7/prices': PRICES })
    renderAt('/trades/7')
    expect(await screen.findByRole('heading', { level: 1, name: /NVDA/ })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'house filing 20035528' })).toHaveAttribute('href', 'https://example.com/f.pdf')
    expect(screen.getByText('-8.9%')).toBeInTheDocument()  // 5-day return
    expect(screen.getByText('-1.5%')).toBeInTheDocument()  // S&P over the same 5 days
    const chart = await screen.findByRole('img', { name: /indexed to 100 at the D0 open/ })
    expect(within(chart).getByText('Traded')).toBeInTheDocument()  // marker before D0
    expect(within(chart).getByText('D0')).toBeInTheDocument()
    expect(screen.getByText('S&P 500 (SPY)')).toBeInTheDocument()
  })

  it('explains a missing D0 price', async () => {
    mockApi({
      ...EMPTY_DB,
      'GET /api/trades/7': TRADE,
      'GET /api/trades/7/prices': { ...PRICES, bars: [{ date: '2026-09-08', price: null, spy: 605, price_adj_open: null, spy_adj_open: 603 }] },
    })
    renderAt('/trades/7')
    expect(await screen.findByText(/No NVDA price on D0/)).toBeInTheDocument()
  })

  it('says when there is no such trade', async () => {
    mockApi(EMPTY_DB)
    renderAt('/trades/999')
    expect(await screen.findByText('No such trade.')).toBeInTheDocument()
  })
})

describe('chart ticks', () => {
  it('are round and include zero', () => {
    expect(ticks(-0.013, 0.032)).toEqual([-0.02, -0.01, 0, 0.01, 0.02, 0.03, 0.04])
    expect(ticks(0.001, 0.0164)).toContain(0)
    expect(ticks(0, 0)).toEqual([0])
  })
})
