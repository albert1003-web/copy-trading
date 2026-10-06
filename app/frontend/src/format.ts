const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
const compactUsd = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', notation: 'compact', minimumFractionDigits: 0, maximumFractionDigits: 1,
})

const usdCents = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' })

export const money = (v: number | null | undefined) => (v == null ? '—' : usd.format(v))

/** A share price, to the cent. */
export const price = (v: number | null | undefined) => (v == null ? '—' : usdCents.format(v))

export const amountRange = (min: number | null, max: number | null) =>
  min == null && max == null ? '—' : `${compactUsd.format(min ?? 0)}–${max == null ? '?' : compactUsd.format(max)}`

export const pct = (v: number | null | undefined, digits = 1) => {
  if (v == null) return '—'
  const text = (v * 100).toFixed(digits)
  if (Number(text) === 0) return `${(0).toFixed(digits)}%`  // no "-0.0%" / "+0.0%"
  return `${v > 0 ? '+' : ''}${text}%`
}

/** "1 day", "5 days". */
export const days = (n: number) => `${n} day${n === 1 ? '' : 's'}`

export const num = (v: number | null | undefined, digits = 2) => (v == null ? '—' : v.toFixed(digits))

export const date = (v: string | null | undefined) => (v ? v.slice(0, 10) : '—')

export const dateTime = (v: string | null | undefined) =>
  v ? new Date(v).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—'

export const today = () => new Date().toISOString().slice(0, 10)
