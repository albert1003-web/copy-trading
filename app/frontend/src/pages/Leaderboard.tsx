import type { Row } from '../api'
import { useApi } from '../hooks'
import { ErrorBanner, Signed, Table } from '../components'
import { date, num, pct } from '../format'

export default function Leaderboard() {
  const board = useApi<Row[]>('/leaderboard')
  const asOf = board.data?.[0]?.as_of

  return (
    <>
      <h1>Leaderboard</h1>
      <p className="muted">
        Members ranked by shrinkage-adjusted abnormal return vs SPY, measured from disclosure.
        {asOf && <> As of {date(asOf)}.</>}
      </p>
      <ErrorBanner error={board.error} />
      <Table
        rows={board.data}
        rowKey={(r) => r.member_id}
        empty="No scores yet. The leaderboard fills in once analytics run (Phase 3)."
        columns={[
          { key: 'rank', label: '#', align: 'right' },
          { key: 'name', label: 'Member' },
          { key: 'party', label: 'Party' },
          { key: 'chamber', label: 'Chamber' },
          { key: 'n_trades', label: 'Trades', align: 'right' },
          { key: 'mean_abn_ret', label: 'Mean abn. return', align: 'right', render: (r) => <Signed value={r.mean_abn_ret}>{pct(r.mean_abn_ret)}</Signed> },
          { key: 'hit_rate', label: 'Hit rate', align: 'right', render: (r) => r.hit_rate == null ? '—' : `${(r.hit_rate * 100).toFixed(0)}%` },
          { key: 'shrunk_score', label: 'Score', align: 'right', render: (r) => num(r.shrunk_score, 3) },
        ]}
      />
    </>
  )
}
