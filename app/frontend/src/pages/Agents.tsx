import { useState } from 'react'
import { api, type Row } from '../api'
import { useApi } from '../hooks'
import { ErrorBanner } from '../components'
import { dateTime } from '../format'

const STATUS: Record<string, string> = { null: 'Pending', 1: 'Approved', 0: 'Rejected' }

export default function Agents() {
  const runs = useApi<Row[]>('/agent-runs')
  const [error, setError] = useState<string | null>(null)

  const decide = async (id: number, approved: boolean) => {
    try { await api.post(`/agent-runs/${id}/decision`, { approved }); runs.reload() }
    catch (err) { setError((err as Error).message) }
  }

  return (
    <>
      <h1>Agents</h1>
      <p className="muted">Digests, research briefs and strategy proposals. Agents never change anything themselves; you approve or reject.</p>
      <ErrorBanner error={error ?? runs.error} />

      {runs.data == null ? <p className="muted">Loading…</p>
        : runs.data.length === 0 ? <div className="empty">No agent runs yet. Agents arrive in Phase 5.</div>
        : runs.data.map((r) => (
          <article key={r.run_id} className="card">
            <div className="card-head">
              <strong>{r.agent}</strong>
              <span className="muted">{dateTime(r.started_at)}</span>
              <span className={`tag ${r.approved === 1 ? 'pos' : r.approved === 0 ? 'neg' : 'warn'}`}>{STATUS[String(r.approved)]}</span>
            </div>
            <pre className="output">{r.output ?? '(no output)'}</pre>
            {r.approved == null && (
              <div className="row">
                <button onClick={() => decide(r.run_id, true)}>Approve</button>
                <button className="ghost" onClick={() => decide(r.run_id, false)}>Reject</button>
              </div>
            )}
          </article>
        ))}
    </>
  )
}
