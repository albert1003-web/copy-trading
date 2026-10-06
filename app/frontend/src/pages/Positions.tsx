import { useState, type ChangeEvent, type FormEvent } from 'react'
import { api, type Row } from '../api'
import { useApi } from '../hooks'
import { ErrorBanner, Signed, Table } from '../components'
import { date, money, pct, price, today } from '../format'

// exitRule undefined = the recommended rule (once the exit backtests have run); '' = don't watch
const EMPTY = { ticker: '', buyDate: today(), buyPrice: '', shares: '', exitRule: undefined as string | undefined, tradeId: '' }

export default function Positions() {
  const positions = useApi<Row[]>('/positions')
  const rules = useApi<Row[]>('/exit-rules')
  const [form, setForm] = useState(EMPTY)
  const [error, setError] = useState<string | null>(null)
  const set = (k: keyof typeof EMPTY) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value })
  const ruleList = rules.data ?? []
  const recommended = ruleList.find((r) => r.recommended)
  const exitRule = form.exitRule ?? recommended?.label ?? ''
  const describe = (label: string | null) => ruleList.find((r) => r.label === label)?.description
  const held = (positions.data ?? []).filter((r) => r.status === 'open' && r.market_value != null)
  const sum = (key: string) => held.reduce((total, r) => total + (r[key] as number), 0)
  const cost = held.reduce((total, r) => total + r.buy_price * r.shares, 0)

  const open = async (e: FormEvent) => {
    e.preventDefault()
    try {
      await api.post('/positions', {
        ticker: form.ticker,
        buyDate: form.buyDate,
        buyPrice: Number(form.buyPrice),
        shares: Number(form.shares),
        exitRule: exitRule || null,
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
      <p className="muted">
        A record of trades you placed by hand in the Roth IRA. Nothing here places orders. Each open position is watched
        for its exit rule, and you get one email when it fires; positions you haven't logged never trigger exit emails.
      </p>
      {recommended && (
        <p className="muted">
          Recommended exit: <strong>{recommended.description}</strong> (confidence {recommended.confidence}: {recommended.reason}).
        </p>
      )}
      <ErrorBanner error={error ?? positions.error} />

      {held.length > 0 && (
        <div className="stats">
          {[
            { label: 'Open cost', value: money(cost) },
            { label: 'Market value', value: money(sum('market_value')) },
            { label: 'Unrealized P&L', value: <Signed value={sum('pnl')}>{money(sum('pnl'))} ({pct(sum('pnl') / cost)})</Signed> },
          ].map((st) => (
            <div key={st.label} className="stat">
              <div className="stat-value">{st.value}</div>
              <div className="stat-label">{st.label}</div>
            </div>
          ))}
        </div>
      )}
      <p className="muted">
        Open positions are valued at the last stored close (updated nightly; dividends not added), closed ones at your
        sell price. "vs S&amp;P 500" is the return minus the S&amp;P 500's over the same days.
      </p>

      <form className="filters" onSubmit={open}>
        <input placeholder="Ticker" value={form.ticker} onChange={set('ticker')} required />
        <input type="date" value={form.buyDate} onChange={set('buyDate')} required />
        <input type="number" step="0.01" min="0" placeholder="Buy price" value={form.buyPrice} onChange={set('buyPrice')} required />
        <input type="number" step="any" min="0" placeholder="Shares" value={form.shares} onChange={set('shares')} required />
        {ruleList.length ? (
          <select aria-label="Exit rule" value={exitRule} onChange={set('exitRule')}>
            {[...ruleList].sort((a, b) => b.recommended - a.recommended).map((r) => (
              <option key={r.label} value={r.label} title={r.description}>
                {r.label}{r.recommended ? ` (recommended · ${r.confidence} confidence)` : ''}
              </option>
            ))}
            <option value="">Don't watch</option>
          </select>
        ) : (
          <input placeholder="Exit rule (optional)" value={form.exitRule ?? ''} onChange={set('exitRule')} />
        )}
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
          {
            key: 'last_close', label: 'Price', align: 'right', render: (r) => r.price_note
              ? <span className="tag warn" title={r.price_note}>check prices</span>
              : r.status === 'open'
                ? <span title={r.last_date ? `Close on ${date(r.last_date)}` : 'No close since the buy yet'}>{price(r.last_close)}</span>
                : price(r.sell_price),
          },
          { key: 'pnl', label: 'P&L', align: 'right', render: (r) => <Signed value={r.pnl}>{money(r.pnl)}</Signed> },
          { key: 'ret', label: 'Return', align: 'right', render: (r) => <Signed value={r.ret}>{pct(r.ret)}</Signed> },
          {
            key: 'excess', label: 'vs S&P 500', align: 'right',
            render: (r) => <Signed value={r.excess}><span title={`S&P 500 ${pct(r.spy_ret)}`}>{pct(r.excess)}</span></Signed>,
          },
          {
            key: 'exit_rule', label: 'Exit rule', render: (r) => (
              <>
                <span title={describe(r.exit_rule)}>{r.exit_rule ?? '—'}</span>
                {r.triggered_on
                  ? <> <span className="tag warn" title={`Exit email sent (${r.exit_reason})`}>triggered {date(r.triggered_on)}</span></>
                  : r.status === 'open' && (describe(r.exit_rule)
                    ? <> <span className="tag">watching</span> <RuleStatus r={r} /></>
                    : <> <span className="muted">not watched</span></>)}
              </>
            ),
          },
          { key: 'sell_date', label: 'Sold', render: (r) => date(r.sell_date) },
          { key: 'close', label: '', render: (r) => r.status === 'open' ? <button className="ghost" onClick={() => close(r)}>Close</button> : '' },
        ]}
      />
    </>
  )
}

/** Where a watched rule stands: "day 3 of 20 · stop $165.20 · target $216.00". */
function RuleStatus({ r }: { r: Row }) {
  const parts = [
    r.max_hold != null && `day ${r.days_held} of ${r.max_hold}`,
    r.stop_level != null && `stop ${price(r.stop_level)}`,
    r.target_level != null && `target ${price(r.target_level)}`,
  ].filter(Boolean)
  return parts.length ? <span className="muted">{parts.join(' · ')}</span> : null
}
