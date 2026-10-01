import { useState, type FormEvent } from 'react'
import { api, type Row } from '../api'
import { useApi } from '../hooks'
import { ErrorBanner, Table } from '../components'
import { dateTime } from '../format'

export default function Watchlist() {
  const watchlist = useApi<Row[]>('/watchlist')
  const members = useApi<Row[]>('/members')
  const [memberId, setMemberId] = useState('')
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)

  const refresh = () => { watchlist.reload(); members.reload() }

  const add = async (e: FormEvent) => {
    e.preventDefault()
    if (!memberId) return
    try {
      await api.post('/watchlist', { memberId, reason: reason || null })
      setMemberId(''); setReason(''); setError(null); refresh()
    } catch (err) { setError((err as Error).message) }
  }

  const remove = async (id: string) => {
    try { await api.del(`/watchlist/${encodeURIComponent(id)}`); refresh() }
    catch (err) { setError((err as Error).message) }
  }

  const unwatched = (members.data ?? []).filter((m) => !m.watched)

  return (
    <>
      <h1>Watchlist</h1>
      <p className="muted">Alerts go out only for members on this list.</p>
      <ErrorBanner error={error ?? watchlist.error ?? members.error} />

      <form className="filters" onSubmit={add}>
        <select value={memberId} onChange={(e) => setMemberId(e.target.value)} disabled={unwatched.length === 0}>
          <option value="">{unwatched.length ? 'Choose a member…' : 'No members loaded yet'}</option>
          {unwatched.map((m) => (
            <option key={m.member_id} value={m.member_id}>{m.name} ({m.party ?? '?'}-{m.state ?? '?'}, {m.chamber})</option>
          ))}
        </select>
        <input placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
        <button type="submit" disabled={!memberId}>Add</button>
      </form>

      <Table
        rows={watchlist.data}
        rowKey={(r) => r.member_id}
        empty="Nobody on the watchlist yet."
        columns={[
          { key: 'name', label: 'Member' },
          { key: 'chamber', label: 'Chamber' },
          { key: 'party', label: 'Party' },
          { key: 'state', label: 'State' },
          { key: 'reason', label: 'Reason' },
          { key: 'added_at', label: 'Added', render: (r) => dateTime(r.added_at) },
          { key: 'remove', label: '', render: (r) => <button className="ghost" onClick={() => remove(r.member_id)}>Remove</button> },
        ]}
      />
    </>
  )
}
