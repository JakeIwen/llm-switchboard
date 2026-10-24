import { TOOLS, type SessionRow } from '../api/sessionRow'
import { plural } from '../format/format'
import { actionPath, hasRemote, hostLock } from '../hosts/hostRow'
import { sessionBlockedReason } from '../sessions/badges'
import { splitBlocked } from '../sessions/bulk'
import { runBulk, summaryItems } from './actionBatch'
import { queueHostAction } from './actionRequests'
import type { ActionContext } from './context'

export async function moveSession(ctx: ActionContext, row: SessionRow): Promise<void> {
  if (!row.host) return moveSessions(ctx, [row.id])
  const reason = hostLock(row)
  if (reason) { ctx.ui.toast({ message: reason }); return }
  if (row.tool === TOOLS.codex) {
    ctx.ui.toast({ message: "Codex can't move a session; fork it into the folder instead." }); return
  }
  const entered = await ctx.ui.form({
    title: `Move “${row.title}”`, confirmLabel: 'Move',
    fields: [{ kind: 'text', key: 'cwd', label: 'Working folder on the Mac', value: row.cwd,
      required: true, autofocus: true, mono: true, maxLength: 4096, suggestions: ctx.knownFolders() }],
  })
  if (!entered) return
  const cwd = String(entered.cwd).trim()
  if (cwd) await queueHostAction(ctx, row, 'move', { cwd })
}

export async function moveSessions(ctx: ActionContext, ids: readonly string[]): Promise<void> {
  const split = splitBlocked<SessionRow>(ids, (id) => ctx.rows().find((row) => row.id === id), sessionBlockedReason)
  const targets = split.targets.filter((row) => row.tool !== TOOLS.codex)
  const codex = split.targets.length - targets.length
  if (!targets.length) {
    const reasons = [...new Set(split.blocked.map(sessionBlockedReason))]
    ctx.ui.toast({ message: codex ? "Codex can't move a session; fork it into the folder instead."
      : reasons.length ? reasons.join(' ') : 'Open in another claude process; close it there first.' })
    return
  }
  const one = targets.length === 1
  const body = [`${one ? 'Its transcript moves' : 'Their transcripts move'} under the new folder, so claudepd --resume works from there.`]
  if (targets.some((row) => row.enabled)) body.push('Kept-running sessions restart in the new folder.')
  if (split.blocked.length) body.push(`${plural(split.blocked.length, 'session')} open, offline or list-only will be skipped.`)
  if (codex) body.push(`${plural(codex, 'Codex session')} will be skipped; Codex can't move one (fork it instead).`)
  const entered = await ctx.ui.form({
    title: one ? `Move “${targets[0].title}”` : `Move ${targets.length} sessions`,
    body, items: summaryItems(targets), confirmLabel: 'Move',
    fields: [{ kind: 'text', key: 'cwd', label: 'Folder', required: true, autofocus: true, mono: true,
      value: targets.every((row) => row.cwd === targets[0].cwd) ? targets[0].cwd : '',
      placeholder: '/home/pi/dev/…', suggestions: ctx.knownFolders() }],
  })
  if (!entered) return
  const cwd = String(entered.cwd).trim()
  if (!cwd) return
  const done = await runBulk(ctx, 'Moving', targets, (id) => ctx.post(actionPath(id, 'move'), { cwd }))
  if (done.length) ctx.ui.toast({ message: hasRemote(done)
    ? 'Local moves done; Mac moves queued. Watch their action status.' : `Moved ${plural(done.length, 'session')} to ${cwd}.` })
}
