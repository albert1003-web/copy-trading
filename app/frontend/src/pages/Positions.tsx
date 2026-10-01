import { useState, type ChangeEvent, type FormEvent } from 'react'
import { api, type Row } from '../api'
import { useApi } from '../hooks'
import { ErrorBanner, Signed, Table } from '../components'
import { date, money, pct, today } from '../format'

const EMPTY = { ticker: '', buyDate: today(), buyPrice: '', shares: '', exitRule: '', tradeId: '' }

export default function Positions() {
  const positions = useApi<Row[]>('/positions')
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState<string | null>(null)
  const set = (k: keyof typeof EMPTY) => (e: ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value })

  const open = async (e: FormEvent) => {
    e.preventDefault()
    try {
      await api.post('/positions', {
        ticker: form.ticker,
        buyDate: form.buyDate,
        buyPrice: Number(form.buyPrice),
        shares: Number(form.shares),
        exitRule: form.exitRule || null,
        tradeId: form.tradeId ? Number(form.tradeId) : null,
      })
      setForm(EMPTY); setError(null); positions.reload()
    } catch (err) { setError((err as Error).message) }
  }

  const close = async (r: Row) => {
    const price = prompt(`Sell price for ${r.ticker}?`)
    if (!price) return
    const sellDate = prompt('Sell date (YYYY-MM-DD)?', today())
    if (!sellDate) return
    try { await api.post(`/positions/${r.position_id}/close`, { sellDate, sellPrice: Number(price) }); positions.reload() }
    catch (err) { setError((err as Error).message) }
  }

  return (
    <>
      <h1>Positions</h1>
      <p className="muted">A record of trades you placed by hand in the Roth IRA. Nothing here places orders.</p>
      <ErrorBanner error={error ?? positions.error} />

      <form className="filters" onSubmit={open}>
        <input placeholder="Ticker" value={form.ticker} onChange={set('ticker')} required />
        <input type="date" value={form.buyDate} onChange={set('buyDate')} required />
        <input type="number" step="0.01" min="0" placeholder="Buy price" value={form.buyPrice} onChange={set('buyPrice')} required />
        <input type="number" step="any" min="0" placeholder="Shares" value={form.shares} onChange={set('shares')} required />
        <input placeholder="Exit rule (optional)" value={form.exitRule} onChange={set('exitRule')} />
        <input type="number" placeholder="Source trade # (optional)" value={form.tradeId} onChange={set('tradeId')} />
        <button type="submit">Log buy</button>
      </form>

      <Table
        rows={positions.data}
        rowKey={(r) => r.position_id}
        empty="No positions logged yet."
        columns={[
          { key: 'ticker', label: 'Ticker', render: (r) => <strong>{r.ticker}</strong> },
          { key: 'status', label: 'Status', render: (r) => <span className="tag">{r.status}</span> },
          { key: 'buy_date', label: 'Bought', render: (r) => date(r.buy_date) },
          { key: 'buy_price', label: 'Buy price', align: 'right', render: (r) => money(r.buy_price) },
          { key: 'shares', label: 'Shares', align: 'right' },
          { key: 'cost', label: 'Cost', align: 'right', render: (r) => money(r.buy_price * r.shares) },
          { key: 'exit_rule', label: 'Exit rule' },
          { key: 'sell_date', label: 'Sold', render: (r) => date(r.sell_date) },
          {
            key: 'ret', label: 'Return', align: 'right', render: (r) => {
              const ret = r.sell_price == null ? null : r.sell_price / r.buy_price - 1
              return <Signed value={ret}>{pct(ret)}</Signed>
            },
          },
          { key: 'close', label: '', render: (r) => r.status === 'open' ? <button className="ghost" onClick={() => close(r)}>Close</button> : '' },
        ]}
      />
    </>
  )
}
