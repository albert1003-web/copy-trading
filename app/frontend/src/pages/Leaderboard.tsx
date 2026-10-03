import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { Row } from '../api'
import { useApi } from '../hooks'
import { ErrorBanner, HorizonPicker, Signed, Table } from '../components'
import { date, pct } from '../format'

const share = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(0)}%`)

export default function Leaderboard() {
  const [horizon, setHorizon] = useState(20)
  const board = useApi<Row[]>(`/leaderboard?horizon=${horizon}`)
  const asOf = board.data?.[0]?.as_of

  return (
    <>
      <h1>Leaderboard</h1>
      <p className="muted">
        How each member's buys did from the first open after disclosure, next to the S&amp;P 500 over the same
        days. Ranked by a shrinkage-adjusted excess return, for members with at least 20 filings; each filing
        counts once.{asOf && <> As of {date(asOf)}.</>}
      </p>
      <div className="filters">
        <HorizonPicker value={horizon} onChange={setHorizon} />
      </div>
      <ErrorBanner error={board.error} />
      <Table
        rows={board.data}
        rowKey={(r) => r.member_id}
        empty="No scores yet. The leaderboard fills in after the nightly analytics run (analytics.leaderboard)."
        columns={[
          { key: 'rank', label: '#', align: 'right', render: (r) => r.rank ?? <span className="muted" title="Fewer than 20 filings">—</span> },
          { key: 'name', label: 'Member', render: (r) => <Link to={`/outcomes?member=${r.member_id}`}>{r.name}</Link> },
          { key: 'party', label: 'Party' },
          { key: 'chamber', label: 'Chamber' },
          { key: 'n_filings', label: 'Filings', align: 'right' },
          { key: 'n_trades', label: 'Trades', align: 'right' },
          { key: 'mean_ret', label: 'Avg return', align: 'right', render: (r) => <Signed value={r.mean_ret}>{pct(r.mean_ret)}</Signed> },
          { key: 'mean_spy_ret', label: 'S&P 500', align: 'right', render: (r) => <Signed value={r.mean_spy_ret}>{pct(r.mean_spy_ret)}</Signed> },
          { key: 'mean_abn_ret', label: 'Excess', align: 'right', render: (r) => <Signed value={r.mean_abn_ret}>{pct(r.mean_abn_ret)}</Signed> },
          { key: 'hit_rate', label: 'Beat S&P', align: 'right', render: (r) => share(r.hit_rate) },
          ...(horizon === 20
            ? [{ key: 'consistency', label: 'Good years', align: 'right' as const, render: (r: Row) => share(r.consistency) }]
            : []),
          { key: 'shrunk_score', label: 'Score', align: 'right', render: (r) => <Signed value={r.shrunk_score}>{pct(r.shrunk_score, 2)}</Signed> },
        ]}
      />
      <p className="note">
        Excess = avg return − S&amp;P 500. Beat S&amp;P = share of filings whose buys beat the index. Good years =
        share of years (3+ filings) with a positive excess. Score = excess pulled toward the average by sample size.
        Free price data omits delisted stocks (survivorship bias).
      </p>
    </>
  )
}
