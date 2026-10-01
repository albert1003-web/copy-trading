import { useCallback, useEffect, useState } from 'react'
import { api } from './api'

/** Fetches a GET endpoint; call `reload()` after a mutation. */
export function useApi<T>(path: string) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(() => {
    api.get<T>(path).then(
      (d) => { setData(d); setError(null) },
      (e: Error) => setError(e.message),
    )
  }, [path])

  useEffect(reload, [reload])
  return { data, error, reload }
}
