const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
const compactUsd = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1,
})

export const money = (v: number | null | undefined) => (v == null ? '—' : usd.format(v))

export const amountRange = (min: number | null, max: number | null) =>
  min == null && max == null ? '—' : `${compactUsd.format(min ?? 0)}–${max == null ? '?' : compactUsd.format(max)}`

export const pct = (v: number | null | undefined, digits = 1) =>
  v == null ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`

export const num = (v: number | null | undefined, digits = 2) => (v == null ? '—' : v.toFixed(digits))

export const date = (v: string | null | undefined) => (v ? v.slice(0, 10) : '—')

export const dateTime = (v: string | null | undefined) =>
  v ? new Date(v).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—'

export const today = () => new Date().toISOString().slice(0, 10)
