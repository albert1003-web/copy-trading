import { useState, type ReactNode } from 'react'

/**
 * Two small SVG charts, drawn by hand (no chart library). Marks follow one spec: thin columns with a rounded data
 * end, 2px lines, hairline grid, a legend for two or more series, and a hover/focus tooltip. Every chart sits next
 * to a table with the same numbers, so the tooltip never gates a value. Colors come from --series-N in styles.css.
 */

export type Series = { name: string; values: (number | null)[] }

const W = 640
const PAD = { top: 12, right: 16, bottom: 28, left: 52 }

/** Round tick values covering [min, max] (always including 0). */
export function ticks(min: number, max: number, count = 5): number[] {
  const lo = Math.min(0, min)
  const hi = Math.max(0, max)
  if (lo === hi) return [0]
  const raw = (hi - lo) / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const out: number[] = []
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toFixed(12)))
  if (out[out.length - 1] < hi) out.push(out[out.length - 1] + step)
  return out
}

function extent(series: Series[]): [number, number] {
  const values = series.flatMap((s) => s.values).filter((v): v is number => v != null && Number.isFinite(v))
  return values.length ? [Math.min(...values), Math.max(...values)] : [0, 0]
}

function Legend({ series }: { series: Series[] }) {
  if (series.length < 2) return null
  return (
    <div className="legend">
      {series.map((s, i) => (
        <span key={s.name}><i className={`swatch series-${i + 1}`} />{s.name}</span>
      ))}
    </div>
  )
}

function Tooltip({ x, title, rows }: { x: number; title: ReactNode; rows: [string, number, ReactNode][] }) {
  const left = `${(x / W) * 100}%`
  return (
    <div className="chart-tip" style={{ left }} role="status">
      <div className="chart-tip-title">{title}</div>
      {rows.map(([name, slot, value]) => (
        <div key={name} className="chart-tip-row">
          <i className={`swatch series-${slot}`} /><span>{name}</span><strong>{value}</strong>
        </div>
      ))}
    </div>
  )
}

/** A column with a 4px rounded data end and a square end on the baseline (works below zero too). */
function columnPath(x: number, base: number, end: number, w: number): string {
  const r = Math.min(4, w / 2, Math.abs(end - base))
  if (end <= base) {  // grows up
    return `M${x},${base} V${end + r} Q${x},${end} ${x + r},${end} H${x + w - r} Q${x + w},${end} ${x + w},${end + r} V${base} Z`
  }
  return `M${x},${base} V${end - r} Q${x},${end} ${x + r},${end} H${x + w - r} Q${x + w},${end} ${x + w},${end - r} V${base} Z`
}

/** Grouped columns per category, from a zero baseline (negative values hang below it). */
export function ColumnChart({ categories, series, format, label, height = 220 }: {
  categories: string[]
  series: Series[]
  format: (v: number) => string
  label: string
  height?: number
}) {
  const [active, setActive] = useState<number | null>(null)
  const [min, max] = extent(series)
  const grid = ticks(min, max)
  const lo = grid[0]
  const hi = grid[grid.length - 1]
  const plotH = height - PAD.top - PAD.bottom
  const y = (v: number) => PAD.top + ((hi - v) / (hi - lo || 1)) * plotH
  const band = (W - PAD.left - PAD.right) / Math.max(categories.length, 1)
  const barW = Math.min(24, (band * 0.6) / series.length)
  const groupW = barW * series.length + 2 * (series.length - 1)

  return (
    <figure className="chart" aria-label={label}>
      <Legend series={series} />
      <div className="chart-box">
        <svg viewBox={`0 0 ${W} ${height}`} role="img" aria-label={label}>
          {grid.map((t) => (
            <g key={t}>
              <line className={t === 0 ? 'axis' : 'grid'} x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
              <text className="tick" x={PAD.left - 8} y={y(t)} dy="0.32em" textAnchor="end">{format(t)}</text>
            </g>
          ))}
          {categories.map((c, i) => {
            const x0 = PAD.left + band * i + (band - groupW) / 2
            return (
              <g key={c}>
                {series.map((s, j) => {
                  const v = s.values[i]
                  if (v == null) return null
                  return <path key={s.name} className={`mark series-${j + 1}`}
                               d={columnPath(x0 + j * (barW + 2), y(0), y(v), barW)} />
                })}
                <text className="tick" x={PAD.left + band * (i + 0.5)} y={height - 8} textAnchor="middle">{c}</text>
                <rect className="hit" x={PAD.left + band * i} y={PAD.top} width={band} height={plotH} tabIndex={0}
                      aria-label={`${c}: ${series.map((s) => `${s.name} ${s.values[i] == null ? 'no data' : format(s.values[i]!)}`).join(', ')}`}
                      onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)}
                      onFocus={() => setActive(i)} onBlur={() => setActive(null)} />
              </g>
            )
          })}
        </svg>
        {active != null && (
          <Tooltip x={PAD.left + band * (active + 0.5)} title={categories[active]}
                   rows={series.map((s, j) => [s.name, j + 1, s.values[active] == null ? '—' : format(s.values[active]!)])} />
        )}
      </div>
    </figure>
  )
}

/** Lines over a shared x axis (dates), with optional vertical markers and a crosshair tooltip. */
export function LineChart({ dates, series, format, label, markers = [], baseline, height = 260 }: {
  dates: string[]
  series: Series[]
  format: (v: number) => string
  label: string
  markers?: { index: number; label: string }[]
  baseline?: number
  height?: number
}) {
  const [active, setActive] = useState<number | null>(null)
  const [min, max] = extent(series)
  const lo0 = Math.min(min, baseline ?? min)
  const hi0 = Math.max(max, baseline ?? max)
  const span = hi0 - lo0 || 1
  const lo = lo0 - span * 0.05
  const hi = hi0 + span * 0.05
  const plotH = height - PAD.top - PAD.bottom - 14  // room for marker labels
  const top = PAD.top + 14
  const y = (v: number) => top + ((hi - v) / (hi - lo)) * plotH
  const plotW = W - PAD.left - PAD.right
  const x = (i: number) => PAD.left + (dates.length > 1 ? (i / (dates.length - 1)) * plotW : plotW / 2)
  const gridTicks = niceRange(lo, hi)
  const xLabels = [...new Set([0, Math.round((dates.length - 1) / 2), dates.length - 1])].filter((i) => i >= 0)

  const path = (values: (number | null)[]) => {
    let d = ''
    let pen = false
    values.forEach((v, i) => {
      if (v == null) { pen = false; return }
      d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)} `
      pen = true
    })
    return d
  }

  const pick = (clientX: number, rect: DOMRect) => {
    const svgX = ((clientX - rect.left) / rect.width) * W
    const i = Math.round(((svgX - PAD.left) / plotW) * (dates.length - 1))
    setActive(Math.max(0, Math.min(dates.length - 1, i)))
  }

  return (
    <figure className="chart" aria-label={label}>
      <Legend series={series} />
      <div className="chart-box">
        <svg viewBox={`0 0 ${W} ${height}`} role="img" aria-label={label}>
          {gridTicks.map((t) => (
            <g key={t}>
              <line className={t === baseline ? 'axis' : 'grid'} x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
              <text className="tick" x={PAD.left - 8} y={y(t)} dy="0.32em" textAnchor="end">{format(t)}</text>
            </g>
          ))}
          {markers.map((m, i) => {
            // labels sit beside their rule (earlier ones to the left, the last to the right) so close markers don't collide
            const last = i === markers.length - 1
            return (
              <g key={m.label}>
                <line className="marker-rule" x1={x(m.index)} x2={x(m.index)} y1={top} y2={top + plotH} />
                <text className="tick" x={x(m.index) + (last ? 4 : -4)} y={PAD.top + 6}
                      textAnchor={last ? 'start' : 'end'}>{m.label}</text>
              </g>
            )
          })}
          {series.map((s, j) => <path key={s.name} className={`line series-${j + 1}`} d={path(s.values)} />)}
          {xLabels.map((i) => (
            <text key={i} className="tick" x={x(i)} y={height - 8}
                  textAnchor={i === 0 ? 'start' : i === dates.length - 1 ? 'end' : 'middle'}>{dates[i]}</text>
          ))}
          {active != null && (
            <g>
              <line className="crosshair" x1={x(active)} x2={x(active)} y1={top} y2={top + plotH} />
              {series.map((s, j) => s.values[active] == null ? null : (
                <circle key={s.name} className={`dot series-${j + 1}`} cx={x(active)} cy={y(s.values[active]!)} r={4} />
              ))}
            </g>
          )}
          <rect className="hit" x={PAD.left} y={top} width={plotW} height={plotH} tabIndex={0}
                aria-label={`${label}. Use the arrow keys to step through days.`}
                onMouseMove={(e) => pick(e.clientX, (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect())}
                onMouseLeave={() => setActive(null)}
                onFocus={() => setActive((a) => a ?? dates.length - 1)} onBlur={() => setActive(null)}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowLeft') setActive((a) => Math.max(0, (a ?? 0) - 1))
                  if (e.key === 'ArrowRight') setActive((a) => Math.min(dates.length - 1, (a ?? 0) + 1))
                }} />
        </svg>
        {active != null && (
          <Tooltip x={x(active)} title={dates[active]}
                   rows={series.map((s, j) => [s.name, j + 1, s.values[active] == null ? '—' : format(s.values[active]!)])} />
        )}
      </div>
    </figure>
  )
}

/** Ticks for a range that needn't include zero. */
function niceRange(lo: number, hi: number, count = 5): number[] {
  const raw = (hi - lo) / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const out: number[] = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toFixed(12)))
  return out
}
