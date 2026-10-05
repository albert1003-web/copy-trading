import { useState } from 'react'
import { api, type Row } from '../api'
import { useApi } from '../hooks'
import { ErrorBanner } from '../components'
import { dateTime } from '../format'

const STATUS: Record<string, string> = { null: 'Pending', 1: 'Approved', 0: 'Rejected' }
const KIND: Record<string, string> = { watchlist_add: 'Watch', watchlist_remove: 'Unwatch', note: 'Note' }

const tone = (approved: number | null) => (approved === 1 ? 'pos' : approved === 0 ? 'neg' : 'warn')

function ProposalStatus({ p }: { p: Row }) {
  if (p.applied_at) {
    const failed = String(p.apply_result ?? '').startsWith('failed')
    return failed
      ? <span className="tag neg" title={p.apply_result}>Apply failed</span>
      : <span className="tag pos">{p.apply_result === 'acknowledged' ? 'Acknowledged' : 'Applied'} {dateTime(p.applied_at)}</span>
  }
  return <span className={`tag ${tone(p.approved)}`}>{p.approved === 1 ? 'Approved, applies on the next pipeline run' : STATUS[String(p.approved)]}</span>
}

function Evidence({ items }: { items: Row[] }) {
  if (!items?.length) return null
  return (
    <details>
      <summary className="muted">Evidence ({items.length})</summary>
      <ul className="evidence">
        {items.map((e, i) => (
          <li key={i}>
            {e.claim}
            {/^https?:\/\//.test(e.source ?? '')
              ? <> (<a href={e.source} target="_blank" rel="noreferrer">source</a>)</>
              : e.source ? <pre className="output">{e.source}</pre> : null}
          </li>
        ))}
      </ul>
    </details>
  )
}

export default function Agents() {
  const runs = useApi<Row[]>('/agent-runs')
  const [error, setError] = useState<string | null>(null)

  const decide = async (path: string, approved: boolean) => {
    try { await api.post(`${path}/decision`, { approved }); setError(null); runs.reload() }
    catch (err) { setError((err as Error).message) }
  }

  return (
    <>
      <h1>Agents</h1>
      <p className="muted">Research briefs, digests and strategy proposals. Agents never change anything themselves: you approve each proposal, and the pipeline applies approved watchlist changes on its next run.</p>
      <ErrorBanner error={error ?? runs.error} />

      {runs.data == null ? <p className="muted">Loading…</p>
        : runs.data.length === 0 ? <div className="empty">No agent runs yet. Ask one with <code>python -m agents.ask "…"</code>.</div>
        : runs.data.map((r) => {
          const proposals: Row[] = r.proposals ?? []
          return (
            <article key={r.run_id} className="card">
              <div className="card-head">
                <strong>{r.agent}</strong>
                <span className="muted">{dateTime(r.started_at)}</span>
                {r.status === 'failed' ? <span className="tag neg">Failed</span>
                  : r.status === 'running' ? <span className="tag warn">Running</span>
                  : proposals.length === 0 && <span className={`tag ${tone(r.approved)}`}>{STATUS[String(r.approved)]}</span>}
              </div>
              {r.status === 'failed' && <p className="neg">{r.error}</p>}
              {r.output && <pre className="output">{r.output}</pre>}
              {r.status !== 'failed' && !r.output && proposals.length === 0 && <pre className="output">(no output)</pre>}

              {proposals.map((p) => (
                <section key={p.proposal_id} className="proposal">
                  <div className="card-head">
                    <span className="tag">{KIND[p.kind] ?? p.kind}</span>
                    <strong>{p.title}</strong>
                    {p.member_name && <span className="muted">{p.member_name}</span>}
                    <ProposalStatus p={p} />
                  </div>
                  <p>{p.rationale}</p>
                  <Evidence items={p.evidence} />
                  {p.approved == null && !p.applied_at && (
                    <div className="row">
                      <button onClick={() => decide(`/agent-proposals/${p.proposal_id}`, true)} aria-label={`Approve: ${p.title}`}>Approve</button>
                      <button className="ghost" onClick={() => decide(`/agent-proposals/${p.proposal_id}`, false)} aria-label={`Reject: ${p.title}`}>Reject</button>
                    </div>
                  )}
                </section>
              ))}

              {proposals.length === 0 && r.status !== 'failed' && r.status !== 'running' && r.approved == null && (
                <div className="row">
                  <button onClick={() => decide(`/agent-runs/${r.run_id}`, true)}>Approve</button>
                  <button className="ghost" onClick={() => decide(`/agent-runs/${r.run_id}`, false)}>Reject</button>
                </div>
              )}
            </article>
          )
        })}
    </>
  )
}
