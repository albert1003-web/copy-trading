import { describe, expect, it } from 'vitest'
import { amountRange, date, days, money, num, pct } from './format'

describe('format', () => {
  it('formats disclosure amount ranges compactly', () => {
    expect(amountRange(1001, 15000)).toBe('$1K–$15K')
    expect(amountRange(1000001, 5000000)).toBe('$1M–$5M')
    expect(amountRange(50000001, null)).toBe('$50M–?')
    expect(amountRange(null, null)).toBe('—')
  })

  it('formats signed percentages', () => {
    expect(pct(0.031)).toBe('+3.1%')
    expect(pct(-0.0125, 2)).toBe('-1.25%')
    expect(pct(0)).toBe('0.0%')
    expect(pct(-0.0003)).toBe('0.0%')  // rounds to zero: no sign
    expect(days(1)).toBe('1 day')
    expect(days(20)).toBe('20 days')
    expect(pct(null)).toBe('—')
  })

  it('formats money, numbers and dates, with a dash for missing values', () => {
    expect(money(1805)).toBe('$1,805')
    expect(money(undefined)).toBe('—')
    expect(num(0.8234)).toBe('0.82')
    expect(date('2026-09-28T14:05:00Z')).toBe('2026-09-28')
    expect(date(null)).toBe('—')
  })
})
