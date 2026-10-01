import type { ReactNode } from 'react'
import type { Row } from './api'

export type Column = {
  key: string
  label: string
  render?: (row: Row) => ReactNode
  align?: 'right'
}

export function Table({ rows, columns, empty, rowKey }: {
  rows: Row[] | null
  columns: Column[]
  empty: ReactNode
  rowKey: (row: Row) => string | number
}) {
  if (rows == null) return <p className="muted">Loading…</p>
  if (rows.length === 0) return <div className="empty">{empty}</div>
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((c) => <th key={c.key} className={c.align}>{c.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((c) => (
                <td key={c.key} className={c.align}>{c.render ? c.render(row) : row[c.key] ?? '—'}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ErrorBanner({ error }: { error: string | null }) {
  return error ? <div className="error">{error}</div> : null
}

export function Action({ value }: { value: string | null }) {
  if (!value) return <>—</>
  const tone = value === 'BUY' ? 'pos' : value.startsWith('SELL') ? 'neg' : ''
  return <span className={`tag ${tone}`}>{value.replace('_', ' ')}</span>
}

export function Signed({ value, children }: { value: number | null | undefined; children: ReactNode }) {
  const tone = value == null ? '' : value > 0 ? 'pos' : value < 0 ? 'neg' : ''
  return <span className={tone}>{children}</span>
}
