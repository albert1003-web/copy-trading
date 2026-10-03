import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { Row } from '../api'
import { useApi } from '../hooks'
import { ColumnChart } from '../charts'
import { ErrorBanner, Signed, Table } from '../components'
import { days, pct } from '../format'

const KS = [1, 2, 3, 5]
const axisPct = (v: number) => `${(v * 100).toFixed(v === 0 ? 0 : 2)}%`

type Data = { stats: Row[]; delays: Row[] }

/** One row per group: filings, trades, mean inflation per k, share cheaper at k=1, best delay. */
function groupRows(data: Data, groupType: string): Row[] {
  return data.delays
    .filter((d) => d.group_type === groupType)
    .map((d) => {
      const row: Row = { ...d }
      for (const s of data.stats) {
        if (s.group_type === groupType && s.group_key === d.group_key) {
          row[`k${s.k}`] = s.mean
          if (s.k === 1) row.share_pos = s.share_pos
        }
      }
      return row
    })
}

const BUCKETS = ['mega', 'large', 'mid', 'small', 'micro', 'unknown']

function BestK({ row }: { row: Row }) {
  if (row.best_k == null) return <span className="muted" title="Fewer than 20 filings">—</span>
  if (row.best_k === 0) return <>D0 open</>
  return <>{days(row.best_k)} <span className="muted">({pct(row.gain, 2)})</span></>
}

function GroupTable({ rows, label }: { rows: Row[]; label: (r: Row) => ReactNode }) {
  return (
    <Table
      rows={rows}
      rowKey={(r) => r.group_key}
      empty="No groups yet."
      columns={[
        { key: 'group_key', label: 'Group', render: label },
        { key: 'n', label: 'Filings', align: 'right' },
        { key: 'n_trades', label: 'Trades', align: 'right' },
        ...KS.map((k) => ({
          key: `k${k}`, label: `Wait ${k}d`, align: 'right' as const,
          render: (r: Row) => <Signed value={r[`k${k}`]}>{pct(r[`k${k}`], 2)}</Signed>,
        })),
        { key: 'share_pos', label: 'Cheaper next day', align: 'right', render: (r) => r.share_pos == null ? '—' : `${(r.share_pos * 100).toFixed(0)}%` },
        { key: 'best_k', label: 'Best entry', render: (r) => <BestK row={r} /> },
      ]}
    />
  )
}

export default function OpenInflation() {
  const res = useApi<Data>('/open-inflation')
  const data = res.data
  const all = data ? groupRows(data, 'all')[0] : undefined

  return (
    <>
      <h1>Open inflation</h1>
      <p className="muted">
        Is the first open after a disclosure (D0) inflated? Open(D0) / Open(D0 + k) − 1 for buys we could copy:
        positive means waiting k trading days was cheaper. Each filing counts once.
      </p>
      <ErrorBanner error={res.error} />
      {data == null ? (
        <p className="muted">Loading…</p>
      ) : !all ? (
        <div className="empty">No open-inflation results yet. They fill in after the nightly analytics run (analytics.open_inflation).</div>
      ) : (
        <>
          <h2>All buys: average saving from waiting k days</h2>
          <ColumnChart
            label="Average open inflation for all copyable buys, by days waited"
            categories={KS.map((k) => `${k}d`)}
            series={[{ name: 'Open inflation', values: KS.map((k) => all[`k${k}`] ?? null) }]}
            format={axisPct}
            height={200}
          />
          <p className="note">
            {all.n} filings. Best entry for all buys: {all.best_k === 0 ? 'the D0 open' : all.best_k == null ? '—' : `wait ${days(all.best_k)}`}.
          </p>
          <h2>By market-cap bucket</h2>
          <GroupTable
            rows={groupRows(data, 'mcap').sort((a, b) => BUCKETS.indexOf(a.group_key) - BUCKETS.indexOf(b.group_key))}
            label={(r) => r.group_key}
          />
          <h2>By media attention</h2>
          <GroupTable rows={groupRows(data, 'attention')} label={(r) => r.group_key === 'high' ? 'High-attention members' : 'Everyone else'} />
          <h2>By member</h2>
          <GroupTable
            rows={groupRows(data, 'member')}
            label={(r) => <Link to={`/outcomes?member=${r.group_key}`}>{r.name ?? r.group_key}</Link>}
          />
          <p className="note">
            Best entry = the delay with the highest shrinkage-adjusted saving, if positive; otherwise the D0 open.
            Groups need 20 filings. Raw open moves include market drift.
          </p>
        </>
      )}
    </>
  )
}
