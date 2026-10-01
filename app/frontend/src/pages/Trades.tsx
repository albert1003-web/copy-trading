import { useState } from 'react'
import type { Row } from '../api'
import { useApi } from '../hooks'
import { Action, ErrorBanner, Table } from '../components'
import { amountRange, date, num } from '../format'

const ACTIONS = ['', 'BUY', 'SELL', 'SELL_PARTIAL', 'EXCHANGE']

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
          { key: 'disclosure_date', label: 'Disclosed', render: (r) => date(r.disclosure_date) },
          { key: 'tx_date', label: 'Traded', render: (r) => date(r.tx_date) },
          { key: 'member_name', label: 'Member', render: (r) => <>{r.member_name ?? '—'} <span className="muted">{r.party ?? ''}</span></> },
          { key: 'ticker', label: 'Ticker', render: (r) => <strong>{r.ticker ?? '—'}</strong> },
          { key: 'asset_name', label: 'Asset', render: (r) => <span className="muted asset" title={r.asset_name ?? ''}>{r.asset_name ?? ''}</span> },
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
