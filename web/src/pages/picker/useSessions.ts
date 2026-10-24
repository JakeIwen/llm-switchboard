import { useCallback, useEffect, useRef, useState } from 'react'
import { getJson } from '../../api/client'
import type { SessionRow } from '../../api/sessionRow'
import type { SessionsPayload, TrashItem } from '../../api/sessionsPayload'

const DEFAULT_POLL_SECONDS = 4

/** Keep last poll's row objects where nothing changed, so memoized rows skip
    rendering and an open row, a focused field or a pressed button survive. */
function stabilize(prev: SessionRow[], next: SessionRow[]): SessionRow[] {
  const byId = new Map(prev.map((row) => [row.id, row]))
  let same = prev.length === next.length
  const rows = next.map((row, i) => {
    const old = byId.get(row.id)
    const keep = old !== undefined && JSON.stringify(old) === JSON.stringify(row)
    if (!keep || prev[i] !== old) same = false
    return keep ? old : row
  })
  return same ? prev : rows
}

export interface SessionsState {
  data: SessionsPayload | null
  trash: TrashItem[]
  /** Why the last poll failed, while the last good list stays shown. */
  error: string
  refresh: () => Promise<void>
}

/** Poll `GET /api/sessions` and `/api/trash` every `poll_seconds` while the
    page is visible, and at once when it becomes visible again. */
export function useSessions(): SessionsState {
  const [data, setData] = useState<SessionsPayload | null>(null)
  const [error, setError] = useState('')
  const [trash, setTrash] = useState<TrashItem[]>([])
  const inFlight = useRef<Promise<void> | null>(null)

  const refresh = useCallback(() => {
    inFlight.current ??= Promise.all([
      getJson<SessionsPayload>('/api/sessions'),
      getJson<{ trash: TrashItem[] }>('/api/trash'),
    ])
      .then(([next, deleted]) => {
        setData((prev) => ({ ...next, sessions: stabilize(prev?.sessions ?? [], next.sessions) }))
        setTrash((prev) => (JSON.stringify(prev) === JSON.stringify(deleted.trash) ? prev : deleted.trash))
        setError('')
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => {
        inFlight.current = null
      })
    return inFlight.current
  }, [])

  const pollSeconds = data?.settings.poll_seconds ?? DEFAULT_POLL_SECONDS
  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, pollSeconds * 1000)
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [refresh, pollSeconds])

  return { data, trash, error, refresh }
}
