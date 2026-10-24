import type { SupervisorState } from '../api/sessionRow'
import type { ActionContext } from './context'

const TROUBLE = new Set<SupervisorState>(['attention', 'backoff', 'error', 'missing'])

/** Run after each successful catalogue refresh, including while viewing transcripts. */
export function checkClaudeReadiness(ctx: ActionContext): void {
  for (const [id, title] of ctx.watching) {
    const row = ctx.rows().find((session) => session.id === id)
    if (!row || !row.enabled) { ctx.watching.delete(id); continue }
    const state = row.status?.state
    if (state === 'running') {
      ctx.watching.delete(id)
      ctx.ui.toast({ message: `“${title}” is ready with Remote Control.`, durationMs: 20000,
        ...(row.remote_url ? { action: { label: 'Open in Claude', href: row.remote_url } } : {}) })
    } else if (state && TROUBLE.has(state)) {
      ctx.watching.delete(id)
      ctx.ui.toast({ message: `“${title}” needs attention: ${row.status!.detail}`, durationMs: 20000,
        action: { label: 'Details', run: () => ctx.reveal(id) } })
    }
  }
}
