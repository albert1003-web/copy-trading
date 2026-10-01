export type Row = Record<string, any>

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`${method} ${path} failed: ${res.status}`)
  return res.status === 204 ? (undefined as T) : res.json()
}

export const api = {
  get: <T = Row[]>(path: string) => request<T>('GET', path),
  post: <T = void>(path: string, body: unknown = {}) => request<T>('POST', path, body),
  del: (path: string) => request<void>('DELETE', path),
}
