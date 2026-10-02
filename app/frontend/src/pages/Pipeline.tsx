import type { Row } from '../api'
import { useApi } from '../hooks'
import { ErrorBanner, Table } from '../components'
import { dateTime } from '../format'

const SLACK_MINUTES = 15

/** Minutes between scheduled runs right now: 30 on weekdays, 120 on weekends (Eastern time, as the pipeline). */
function scheduleMinutes(now: Date) {
  const day = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short' }).format(now)
  return day === 'Sat' || day === 'Sun' ? 120 : 30
}

function ago(minutes: number) {
  if (minutes < 1) return 'just now'
  if (minutes < 90) return `${Math.round(minutes)} min ago`
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`
  return `${Math.round(minutes / 1440)} days ago`
}

type StageRecord = { ok: boolean; errors?: string[]; summary?: Record<string, unknown> }

function stagesOf(run: Row): Record<string, StageRecord> {
  try { return JSON.parse(run.stages ?? '{}') } catch { return {} }
}

function warningsOf(run: Row): string[] {
  try { return JSON.parse(run.warnings ?? '[]') } catch { return [] }
}

const label = (stage: string) => stage.replace('_', ' ')

/** What a run found: new filings, parsed rows, alert emails. */
function found(run: Row) {
  const st = stagesOf(run)
  const n = (stage: string, key: string) => Number(st[stage]?.summary?.[key] ?? 0)
  const filings = n('ingest_house', 'new_from_index') + n('ingest_house', 'new_from_search') + n('ingest_senate', 'new')
  const parts = []
  if (filings) parts.push(`${filings} new filing${filings === 1 ? '' : 's'}`)
  if (n('parse', 'rows')) parts.push(`${n('parse', 'rows')} trades parsed`)
  if (n('alerts', 'emails')) parts.push(`${n('alerts', 'emails')} alert email${n('alerts', 'emails') === 1 ? '' : 's'}`)
  return parts.join(' · ') || <span className="muted">nothing new</span>
}

function Health({ health }: { health: Row }) {
  const last = health.last_run
  if (!last) {
    return (
      <div className="card">
        <p className="muted">The pipeline hasn't run yet. Install the schedule: <code>python -m pipeline.schedule install</code></p>
      </div>
    )
  }
  const now = new Date()
  const minutes = (now.getTime() - new Date(last.started_at).getTime()) / 60000
  const stale = minutes > scheduleMinutes(now) + SLACK_MINUTES
  const failing = Object.entries(stagesOf(last)).filter(([, r]) => !r.ok).map(([name]) => label(name))
  const warnings = warningsOf(last)

  return (
    <div className="card">
      <span className={last.status === 'failed' ? 'neg' : last.status === 'ok' ? 'pos' : 'muted'}>
        Last run {ago(minutes)} · {last.status}
      </span>
      {failing.length > 0 && (
        <p className="neg">Failing: {failing.join(', ')} ({health.failure_streak} run{health.failure_streak === 1 ? '' : 's'} in a row)</p>
      )}
      {stale && <p className="warn-text">No runs for {ago(minutes).replace(' ago', '')}. Is the Mac asleep, or the schedule not installed?</p>}
      <p className="muted">
        Last success: {dateTime(health.last_ok_at)} · {health.runs_24h} run{health.runs_24h === 1 ? '' : 's'} in the last 24 h
        {warnings.length > 0 && <> · {warnings.length} warning{warnings.length === 1 ? '' : 's'}: {warnings.join('; ')}</>}
      </p>
    </div>
  )
}

/** Price coverage and the parse/review queue (history backfill, nightly prices). */
function History({ history }: { history: Row }) {
  const cov = history.price_coverage ?? {}
  const symbols = Number(cov.ok ?? 0) + Number(cov.partial ?? 0) + Number(cov.missing ?? 0)
  const queue: Row[] = history.review_queue ?? []
  const pct = history.trades_with_symbol ? Math.round((100 * history.trades_priced) / history.trades_with_symbol) : 0
  return (
    <>
      <h2>Prices</h2>
      <div className="card">
        {symbols === 0
          ? <p className="muted">Prices haven't been fetched yet. They update each weekday evening, or run <code>python -m prices.fetch --all</code>.</p>
          : <p>
              {symbols} symbols: <span className="pos">{cov.ok} ok</span> · <span className="warn-text">{cov.partial} partial</span> · <span className="neg">{cov.missing} missing</span>
              {' · '}{history.trades_priced} of {history.trades_with_symbol} trades fully priced ({pct}%)
              <br /><span className="muted">Last nightly run: {history.last_nightly ?? 'never'}. Gaps: <code>python -m prices.fetch --gaps</code>. Free data lacks delisted tickers (survivorship bias).</span>
            </p>}
      </div>
      <h2>Filings by year</h2>
      <Table
        rows={queue}
        rowKey={(r) => `${r.chamber}-${r.year}`}
        empty="No filings yet. Backfill history with python -m pipeline.backfill --from 2020."
        columns={[
          { key: 'year', label: 'Year' },
          { key: 'chamber', label: 'Chamber' },
          { key: 'filings', label: 'Filings', align: 'right' },
          { key: 'parsed', label: 'Parsed', align: 'right' },
          { key: 'scanned', label: 'Scanned (review)', align: 'right' },
          { key: 'needs_review', label: 'Needs review', align: 'right' },
          { key: 'failed', label: 'Failed', align: 'right', render: (r) => r.failed ? <span className="neg">{r.failed}</span> : 0 },
          { key: 'pending', label: 'Pending', align: 'right' },
        ]}
      />
    </>
  )
}

export default function Pipeline() {
  const health = useApi<Row>('/pipeline/health')
  const runs = useApi<Row[]>('/pipeline/runs?limit=50')
  const history = useApi<Row>('/pipeline/history')

  return (
    <>
      <h1>Pipeline</h1>
      <p className="muted">Runs every 30 minutes on weekdays and every 2 hours on weekends while the Mac is awake.</p>
      <ErrorBanner error={health.error ?? runs.error ?? history.error} />
      {health.data && <Health health={health.data} />}
      {history.data && <History history={history.data} />}

      <h2>Recent runs</h2>
      <Table
        rows={runs.data}
        rowKey={(r) => r.run_id}
        empty="No runs yet."
        columns={[
          { key: 'started_at', label: 'Started', render: (r) => dateTime(r.started_at) },
          { key: 'status', label: 'Status', render: (r) => <span className={`tag ${r.status === 'ok' ? 'pos' : r.status === 'failed' ? 'neg' : ''}`}>{r.status}</span> },
          { key: 'found', label: 'Found', render: found },
          {
            key: 'problems', label: 'Problems', render: (r) => {
              const errors = Object.values(stagesOf(r)).flatMap((s) => s.errors ?? [])
              const warnings = warningsOf(r)
              if (!errors.length && !warnings.length) return ''
              return <>
                {errors.map((e) => <div key={e} className="neg">{e}</div>)}
                {warnings.map((w) => <div key={w} className="warn-text">{w}</div>)}
              </>
            },
          },
        ]}
      />
    </>
  )
}
