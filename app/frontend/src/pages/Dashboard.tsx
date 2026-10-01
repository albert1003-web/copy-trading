import type { Row } from '../api'
import { useApi } from '../hooks'
import { Action, ErrorBanner, Table } from '../components'
import { amountRange, date, dateTime, num } from '../format'

export default function Dashboard() {
  const summary = useApi<Row>('/summary')
  const filings = useApi<Row[]>('/filings/recent?limit=10')
  const alerts = useApi<Row[]>('/alerts/recent?limit=10')
  const s = summary.data

  const stats = [
    { label: 'Filings', value: s?.filings },
    { label: 'Trades', value: s?.trades },
    { label: 'Alerts sent', value: s?.alerts },
    { label: 'Watching', value: s?.watchlist },
    { label: 'Open positions', value: s?.open_positions },
    { label: 'Needs review', value: s?.needs_review },
  ]

  return (
    <>
      <h1>Dashboard</h1>
      <ErrorBanner error={summary.error ?? filings.error ?? alerts.error} />
      <p className="muted">Last new filing seen: {dateTime(s?.last_filing_seen_at)}</p>

      <div className="stats">
        {stats.map((st) => (
          <div key={st.label} className="stat">
            <div className="stat-value">{st.value ?? '—'}</div>
            <div className="stat-label">{st.label}</div>
          </div>
        ))}
      </div>

      <h2>Recent alerts</h2>
      <Table
        rows={alerts.data}
        rowKey={(r) => r.alert_id}
        empty="No alerts yet. They appear once the alert pipeline (Phase 1) sends its first email."
        columns={[
          { key: 'sent_at', label: 'Sent', render: (r) => dateTime(r.sent_at) },
          { key: 'member_name', label: 'Member' },
          { key: 'ticker', label: 'Ticker' },
          { key: 'action', label: 'Action', render: (r) => <Action value={r.action} /> },
          { key: 'amount', label: 'Amount', render: (r) => amountRange(r.amount_min, r.amount_max) },
          { key: 'score', label: 'Score', align: 'right', render: (r) => num(r.score) },
          { key: 'suggested_exit', label: 'Suggested exit' },
        ]}
      />

      <h2>Recent filings</h2>
      <Table
        rows={filings.data}
        rowKey={(r) => r.doc_id}
        empty="No filings yet. Run the ingestion pipeline to fill the database."
        columns={[
          { key: 'first_seen_at', label: 'First seen', render: (r) => dateTime(r.first_seen_at) },
          { key: 'member_name', label: 'Member' },
          { key: 'chamber', label: 'Chamber' },
          { key: 'state_district', label: 'District' },
          { key: 'filing_date', label: 'Filed', render: (r) => date(r.filing_date) },
          { key: 'doc_format', label: 'Format', render: (r) => r.doc_format === 'scanned' ? <span className="tag warn">scanned</span> : r.doc_format ?? '—' },
          { key: 'parse_status', label: 'Status', render: (r) => <span className={`tag ${r.parse_status === 'needs_review' ? 'warn' : ''}`}>{r.parse_status.replace('_', ' ')}</span> },
          { key: 'source_url', label: 'Source', render: (r) => r.source_url ? <a href={r.source_url} target="_blank" rel="noreferrer">View</a> : '—' },
        ]}
      />
    </>
  )
}
