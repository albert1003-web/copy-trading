import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { Row } from '../api'
import { useApi } from '../hooks'
import { Action, ErrorBanner, Table } from '../components'
import { amountRange, date, num } from '../format'

const ACTIONS = ['', 'BUY', 'SELL', 'SELL_PARTIAL', 'EXCHANGE']

/** The symbol to trade, plus a note when it isn't the ticker as filed or isn't on NYSE/Nasdaq. */
function TickerCell({ row }: { row: Row }) {
  const symbol = row.symbol ?? row.ticker
  if (!symbol) return <>—</>
  return (
    <>
      <Link to={`/trades/${row.trade_id}`}><strong>{symbol}</strong></Link>
      {row.ticker_status === 'renamed' && <span className="muted"> (filed as {row.ticker})</span>}
      {row.ticker_status === 'unlisted' && <> <span className="tag warn" title="Not on NYSE/Nasdaq: OTC or an old symbol">unlisted</span></>}
    </>
  )
}

/** Sector, plus a tag when the member sat on a committee overseeing it (in the Congress of the trade). */
function SectorCell({ row }: { row: Row }) {
  if (!row.sector) return <span className="muted">—</span>
  return (
    <>
      <span title={row.industry ?? ''}>{row.sector}</span>
      {row.committee_relevant === 1 && <> <span className="tag" title="The member sat on a committee overseeing this sector">committee</span></>}
    </>
  )
}

/** Disclosure date; "est." when the filing was backfilled, so D0 is estimated from the filing date. */
function DisclosedCell({ row }: { row: Row }) {
  return (
    <>
      {date(row.disclosure_date)}
      {row.available_basis === 'filed' && <span className="muted" title="Backfilled: not seen live, so D0 is estimated from the filing date"> est.</span>}
    </>
  )
}

export default function Trades() {
  const [member, setMember] = useState('')
  const [ticker, setTicker] = useState('')
  const [action, setAction] = useState('')
  const query = new URLSearchParams({ member, ticker, action }).toString()
  const trades = useApi<Row[]>(`/trades?${query}`)

  return (
    <>
      <h1>Trades</h1>
      <div className="filters">
        <input placeholder="Member name" value={member} onChange={(e) => setMember(e.target.value)} />
        <input placeholder="Ticker" value={ticker} onChange={(e) => setTicker(e.target.value)} />
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          {ACTIONS.map((a) => <option key={a} value={a}>{a ? a.replace('_', ' ') : 'Any action'}</option>)}
        </select>
      </div>
      <ErrorBanner error={trades.error} />
      <Table
        rows={trades.data}
        rowKey={(r) => r.trade_id}
        empty="No trades match. If this is a new install, run the ingestion and parsing pipelines first."
        columns={[
          { key: 'disclosure_date', label: 'Disclosed', render: (r) => <DisclosedCell row={r} /> },
          { key: 'tx_date', label: 'Traded', render: (r) => date(r.tx_date) },
          { key: 'member_name', label: 'Member', render: (r) => <>{r.member_name ?? '—'} <span className="muted">{r.party ?? ''}</span></> },
          { key: 'ticker', label: 'Ticker', render: (r) => <TickerCell row={r} /> },
          { key: 'asset_name', label: 'Asset', render: (r) => <span className="muted asset" title={r.asset_name ?? ''}>{r.asset_name ?? ''}</span> },
          { key: 'sector', label: 'Sector', render: (r) => <SectorCell row={r} /> },
          { key: 'mcap_bucket', label: 'Size', render: (r) => r.mcap_bucket ?? '' },
          { key: 'action', label: 'Action', render: (r) => <Action value={r.action} /> },
          { key: 'owner', label: 'Owner' },
          { key: 'amount', label: 'Amount', render: (r) => amountRange(r.amount_min, r.amount_max) },
          { key: 'filing_delay_days', label: 'Delay (d)', align: 'right' },
          { key: 'score', label: 'Score', align: 'right', render: (r) => num(r.score) },
          { key: 'source_url', label: '', render: (r) => r.source_url ? <a href={r.source_url} target="_blank" rel="noreferrer">Filing</a> : '' },
        ]}
      />
    </>
  )
}
