import { vi } from 'vitest'

type Handler = unknown | ((body: any, url: URL) => unknown)

/**
 * Stubs `fetch` for /api calls. Keys are "METHOD /api/path" (query string ignored);
 * values are the JSON to return, or a function of the request body.
 * Unknown routes return 404; a value of `Error` returns 500.
 */
export function mockApi(routes: Record<string, Handler>) {
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost')
    const key = `${init?.method ?? 'GET'} ${url.pathname}`
    if (!(key in routes)) return new Response(null, { status: 404 })
    const handler = routes[key]
    if (handler === Error) return new Response(null, { status: 500 })
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    const result = typeof handler === 'function' ? (handler as (b: any, u: URL) => unknown)(body, url) : handler
    return result === undefined
      ? new Response(null, { status: 204 })
      : new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Every GET endpoint the app reads, returning an empty database. */
export const EMPTY_DB = {
  'GET /api/summary': {
    filings: 0, needs_review: 0, trades: 0, alerts: 0, watchlist: 0, open_positions: 0, last_filing_seen_at: null,
  },
  'GET /api/pipeline/health': { last_run: null, last_ok_at: null, failure_streak: 0, runs_24h: 0 },
  'GET /api/pipeline/runs': [],
  'GET /api/filings/recent': [],
  'GET /api/alerts/recent': [],
  'GET /api/trades': [],
  'GET /api/members': [],
  'GET /api/watchlist': [],
  'GET /api/leaderboard': [],
  'GET /api/positions': [],
  'GET /api/agent-runs': [],
}
