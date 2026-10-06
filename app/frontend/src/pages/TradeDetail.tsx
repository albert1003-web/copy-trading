import type { ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { Row } from '../api'
import { useApi } from '../hooks'
import { LineChart } from '../charts'
import { Action, ErrorBanner, HORIZONS, Signed, Table, VISION_NOTE } from '../components'
import { amountRange, date, dateTime, days, money, pct } from '../format'

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return <div><span>{label}</span><span>{children}</span></div>
}

/** The symbol and SPY indexed to 100 at the D0 open, so the line's height is the outcome return. */
function PriceChart({ id }: { id: string }) {
  const res = useApi<Row>(`/trades/${id}/prices`)
  const prices = res.data
  if (res.error) return <ErrorBanner error={res.error} />
  if (!prices) return <p className="muted">Loading…</p>
  const bars: Row[] = prices.bars
  const d0 = bars.findIndex((b) => b.date === prices.d0_date)
  const base = d0 >= 0 ? bars[d0] : undefined
  if (!prices.symbol || !base?.price_adj_open || !base?.spy_adj_open) {
    return (
      <div className="empty">
        {!prices.symbol ? 'No validated ticker for this trade, so there are no prices.'
          : !prices.d0_date ? 'D0 hasn\'t happened yet (or prices haven\'t been fetched).'
            : `No ${prices.symbol} price on D0 (free price data lacks delisted tickers).`}
      </div>
    )
  }
  const index = (v: number | null, b: number) => (v == null ? null : (100 * v) / b)
  const traded = bars.findIndex((b) => b.date >= (prices.tx_date ?? '9999'))
  const markers = [{ index: d0, label: 'D0' }]
  if (traded >= 0 && traded < d0) markers.unshift({ index: traded, label: 'Traded' })
  return (
    <LineChart
      label={`${prices.symbol} and the S&P 500, indexed to 100 at the D0 open`}
      dates={bars.map((b) => b.date)}
      series={[
        { name: prices.symbol, values: bars.map((b) => index(b.price, base.price_adj_open)) },
        { name: 'S&P 500 (SPY)', values: bars.map((b) => index(b.spy, base.spy_adj_open)) },
      ]}
      markers={markers}
      baseline={100}
      format={(v) => v.toFixed(0)}
    />
  )
}

export default function TradeDetail() {
  const { id = '' } = useParams()
  const res = useApi<Row>(`/trades/${id}`)
  const t = res.data

  if (res.error) {
    return (
      <>
        <h1>Trade</h1>
        <ErrorBanner error={res.error.includes('404') ? 'No such trade.' : res.error} />
        <Link to="/trades">Back to trades</Link>
      </>
    )
  }
  if (!t) return <p className="muted">Loading…</p>

  const horizons = HORIZONS.map((h) => ({
    horizon: h, ret: t[`ret_${h}`], abn: t[`abn_ret_${h}`], win: t[`win_${h}`],
    spy: t[`ret_${h}`] == null || t[`abn_ret_${h}`] == null ? null : t[`ret_${h}`] - t[`abn_ret_${h}`],
  }))

  return (
    <>
      <p className="muted"><Link to="/trades">Trades</Link> / {t.trade_id}</p>
      <h1>{t.symbol ?? t.ticker ?? 'No ticker'} <Action value={t.action} /></h1>
      <p className="muted">{t.asset_name}</p>
      {t.parse_method === 'vision' && <p className="note">{VISION_NOTE}</p>}

      <div className="facts card">
        <Fact label="Member">{t.member_name ?? '—'} {t.party && <span className="muted">({t.party})</span>}</Fact>
        <Fact label="Owner">{t.owner ?? '—'}</Fact>
        <Fact label="Amount">{amountRange(t.amount_min, t.amount_max)}</Fact>
        <Fact label="Sector">{t.sector ?? '—'}{t.mcap_bucket && <span className="muted"> · {t.mcap_bucket}</span>}</Fact>
        <Fact label="Traded">{date(t.tx_date)}</Fact>
        <Fact label="Disclosed">{date(t.disclosure_date)} <span className="muted">({t.filing_delay_days ?? '?'} days later)</span></Fact>
        <Fact label="Available to us">
          {t.available_basis === 'filed' ? <>{date(t.filing_date)} <span className="muted">(est., backfilled)</span></> : dateTime(t.available_at)}
        </Fact>
        <Fact label="D0 (first open after)">{date(t.d0_date)}{t.d0_open != null && <span className="muted"> at {money(t.d0_open)}</span>}</Fact>
        <Fact label="Filing">{t.source_url ? <a href={t.source_url} target="_blank" rel="noreferrer">{t.chamber} filing {t.doc_id}</a> : t.doc_id}</Fact>
      </div>
      {t.description && <p className="note">{t.description}</p>}

      <h2>Price vs the S&amp;P 500 (100 = D0 open)</h2>
      <PriceChart id={id} />

      <h2>Outcome from D0</h2>
      {t.d0_date == null ? (
        <div className="empty">{t.parse_method === 'vision'
          ? 'Not measured: trades read from scanned filings are kept out of analytics.'
          : "No outcome yet: the trade has no priced ticker, or D0 hasn't happened."}</div>
      ) : (
        <>
          <Table
            rows={horizons}
            rowKey={(r) => r.horizon}
            empty=""
            columns={[
              { key: 'horizon', label: 'Held', render: (r) => days(r.horizon) },
              { key: 'ret', label: 'Return', align: 'right', render: (r) => <Signed value={r.ret}>{pct(r.ret)}</Signed> },
              { key: 'spy', label: 'S&P 500', align: 'right', render: (r) => <Signed value={r.spy}>{pct(r.spy)}</Signed> },
              { key: 'abn', label: 'Excess', align: 'right', render: (r) => <Signed value={r.abn}>{pct(r.abn)}</Signed> },
              { key: 'win', label: 'Beat S&P', render: (r) => r.ret == null ? <span className="muted">pending</span> : r.win == null ? <span className="muted">n/a</span> : r.win ? 'yes' : 'no' },
            ]}
          />
          <p className="note">
            {t.copyable ? '' : 'Not a buy we could copy, so no win/loss label. '}
            Before D0 (context only): {pct(t.tx_ret)} ({pct(t.tx_abn_ret)} vs the S&amp;P 500).
            Open inflation: {[1, 2, 3, 5].map((k) => `${k}d ${pct(t[`open_infl_${k}`], 2)}`).join(' · ')}.
            {t.complete ? ' Complete (60 trading days).' : ''}
          </p>
        </>
      )}
    </>
  )
}
