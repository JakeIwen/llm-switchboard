import { useEffect, useMemo, useRef } from 'react'
import type { ActionUi } from '../../actions/ui'
import type { SessionsPayload } from '../../api/sessionsPayload'
import type { BadgeTone } from '../../sessions/badges'

export interface RowNotice {
  label: string
  tone: BadgeTone
  title?: string
}

// The server keeps a longer history than the page needs.
const RECENT_S = 600
// A finished action's pill stays this long; its toast already said the rest.
const FINISHED_S = 60
const NOTICE: Record<string, (verb: string) => RowNotice> = {
  queued: (verb) => ({ label: `${verb} queued`, tone: 'info' }),
  sent: (verb) => ({ label: `${verb} delivered`, tone: 'info', title: 'Delivered to the Mac; waiting for its result' }),
  done: (verb) => ({ label: `${verb} done`, tone: 'ok' }),
  failed: (verb) => ({ label: `${verb} failed`, tone: 'bad' }),
  expired: (verb) => ({ label: `${verb} expired`, tone: 'warn' }),
}
const FINISHED = new Set(['done', 'failed', 'expired'])
const verbOf = (action: string) => {
  const words = action.replace('-', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** Mac actions as a pill on the affected row, and a toast when one finishes.
    Replaces the old page's status box above the list (docs/design.md). */
export function useHostActions(data: SessionsPayload | null, ui: ActionUi): ReadonlyMap<string, RowNotice> {
  const seen = useRef<Map<string, string> | null>(null)
  const actions = useMemo(() => {
    if (!data) return []
    return data.host_actions.filter((a) => a.queued_at > data.now - RECENT_S)
  }, [data])

  useEffect(() => {
    if (!data) return
    const first = seen.current === null
    const known = seen.current ?? new Map<string, string>()
    for (const a of actions) {
      const before = known.get(a.id)
      known.set(a.id, a.state)
      if (first || before === a.state || !FINISHED.has(a.state)) continue
      const what = `${verbOf(a.action)} of “${a.title}” on ${a.host}`
      ui.toast({
        message: a.state === 'done' ? `${what}: done.` : `${what} ${a.state}${a.message ? `: ${a.message}` : '.'}`,
        tone: a.state === 'done' ? 'info' : 'error',
        durationMs: a.state === 'done' ? 5000 : 15000,
      })
    }
    seen.current = known
  }, [actions, data, ui])

  return useMemo(() => {
    const byRow = new Map<string, RowNotice>()
    const now = data?.now ?? 0
    // Oldest first, so the latest action for a row wins.
    for (const a of [...actions].sort((x, y) => x.queued_at - y.queued_at)) {
      if (FINISHED.has(a.state) && a.queued_at < now - FINISHED_S) continue
      const notice = (NOTICE[a.state] ?? ((verb: string) => ({ label: `${verb} ${a.state}`, tone: 'idle' as const })))(verbOf(a.action))
      byRow.set(`${a.host}:${a.session_id}`, a.message ? { ...notice, title: a.message } : notice)
    }
    return byRow
  }, [actions, data])
}
