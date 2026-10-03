import { Link, useSearchParams } from 'react-router-dom'
import type { Row } from '../api'
import { useApi } from '../hooks'
import { ColumnChart } from '../charts'
import { ErrorBanner, Signed, Table } from '../components'
import { date, days, pct } from '../format'

const share = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(0)}%`)
const axisPct = (v: number) => `${(v * 100).toFixed(Math.abs(v) < 0.1 && v !== 0 ? 1 : 0)}%`

function Ret({ value }: { value: number | null | undefined }) {
  return <Signed value={value}>{pct(value)}</Signed>
}

export default function Outcomes() {
  const [params, setParams] = useSearchParams()
  const member = params.get('member') ?? ''
  const members = useApi<Row[]>('/outcomes/members')
  const summary = useApi<Row[]>(`/outcomes/summary?member=${encodeURIComponent(member)}`)
  const trades = useApi<Row[]>(`/outcomes/trades?member=${encodeURIComponent(member)}`)
  const rows = summary.data ?? []
  const who = member ? members.data?.find((m) => m.member_id === member)?.name ?? member : 'All members'

  return (
    <>
      <h1>Outcomes</h1>
      <p className="muted">
        Buys we could copy (stocks and bought calls), measured from the first open after disclosure (D0), against
        the S&amp;P 500 over the same days. Each filing counts once.
      </p>
      <div className="filters">
        <select aria-label="Member" value={member}
                onChange={(e) => setParams(e.target.value ? { member: e.target.value } : {})}>
          <option value="">All members</option>
          {(members.data ?? []).map((m) => (
            <option key={m.member_id} value={m.member_id}>{m.name} ({m.n_filings})</option>
          ))}
        </select>
      </div>
      <ErrorBanner error={summary.error ?? trades.error ?? members.error} />

      {summary.data != null && rows.length === 0 ? (
        <div className="empty">No outcomes yet. They fill in after the nightly analytics run (analytics.outcomes).</div>
      ) : summary.data == null ? (
        <p className="muted">Loading…</p>
      ) : (
        <>
          <h2>{who}: average return vs the S&amp;P 500, by trading days held</h2>
          <ColumnChart
            label={`${who}: average return of buys and of the S&P 500, by horizon`}
            categories={rows.map((r) => `${r.horizon}d`)}
            series={[
              { name: 'Buys', values: rows.map((r) => r.mean_ret) },
              { name: 'S&P 500', values: rows.map((r) => r.mean_spy_ret) },
            ]}
            format={axisPct}
          />
          <Table
            rows={rows}
            rowKey={(r) => r.horizon}
            empty=""
            columns={[
              { key: 'horizon', label: 'Held', render: (r) => days(r.horizon) },
              { key: 'n_filings', label: 'Filings', align: 'right' },
              { key: 'n_trades', label: 'Trades', align: 'right' },
              { key: 'mean_ret', label: 'Avg return', align: 'right', render: (r) => <Ret value={r.mean_ret} /> },
              { key: 'mean_spy_ret', label: 'S&P 500', align: 'right', render: (r) => <Ret value={r.mean_spy_ret} /> },
              { key: 'mean_abn_ret', label: 'Excess', align: 'right', render: (r) => <Ret value={r.mean_abn_ret} /> },
              { key: 'hit_rate', label: 'Beat S&P', align: 'right', render: (r) => share(r.hit_rate) },
            ]}
          />
        </>
      )}

      <h2>Trades</h2>
      <Table
        rows={trades.data}
        rowKey={(r) => r.trade_id}
        empty="No buys with outcomes for this selection yet."
        columns={[
          { key: 'd0_date', label: 'D0', render: (r) => date(r.d0_date) },
          { key: 'member_name', label: 'Member' },
          { key: 'symbol', label: 'Ticker', render: (r) => <Link to={`/trades/${r.trade_id}`}>{r.symbol ?? '—'}</Link> },
          { key: 'ret_5', label: '5d', align: 'right', render: (r) => <Ret value={r.ret_5} /> },
          { key: 'ret_20', label: '20d', align: 'right', render: (r) => <Ret value={r.ret_20} /> },
          { key: 'ret_60', label: '60d', align: 'right', render: (r) => <Ret value={r.ret_60} /> },
          { key: 'abn_ret_20', label: 'Excess 20d', align: 'right', render: (r) => <Ret value={r.abn_ret_20} /> },
          { key: 'win_20', label: 'Beat S&P (20d)', render: (r) => r.win_20 == null ? <span className="muted">pending</span> : r.win_20 ? 'yes' : 'no' },
          { key: 'tx_abn_ret', label: 'Before D0', align: 'right', render: (r) => <span className="muted" title="Excess move from the trade date to D0: context only, not tradeable">{pct(r.tx_abn_ret)}</span> },
        ]}
      />
      <p className="note">
        Before D0 = the excess move between the member's trade and the first open we could act on (context only).
        Free price data omits delisted stocks (survivorship bias).
      </p>
    </>
  )
}
