import type { SessionRow } from '../api/sessionRow'
import { plural } from '../format/format'
import { actionPath, hasRemote, hostLock } from '../hosts/hostRow'
import { sessionBlockedReason } from '../sessions/badges'
import { splitBlocked } from '../sessions/bulk'
import { runBulk, summaryItems } from './actionBatch'
import { queueHostAction } from './actionRequests'
import type { ActionContext, TrashSession } from './context'

export async function deleteSession(ctx: ActionContext, row: SessionRow): Promise<void> {
  if (!row.host) return deleteSessions(ctx, [row.id])
  const locked = hostLock(row)
  if (locked) { ctx.ui.toast({ message: locked }); return }
  if (!await ctx.ui.confirm({
    title: `Delete “${row.title}” on the Mac?`,
    body: ["Queues a move to the Mac's trash for 30 days. No transcript is uploaded."],
    confirmLabel: 'Queue delete', danger: true,
  })) return
  await queueHostAction(ctx, row, 'delete', {})
}

export async function deleteSessions(ctx: ActionContext, ids: readonly string[]): Promise<void> {
  const { targets, blocked } = splitBlocked<SessionRow>(ids, (id) => ctx.rows().find((row) => row.id === id), sessionBlockedReason)
  if (!targets.length) {
    ctx.ui.toast({ message: 'Selected sessions are open, offline or list-only; close them or wait for the Mac.' }); return
  }
  const kept = targets.filter((row) => row.enabled).length
  const busy = targets.filter((row) => row.status?.busy).length
  const trashable = targets.filter((row) => !row.new || row.host)
  const one = targets.length === 1
  const body: string[] = []
  if (trashable.length === targets.length) body.push(`${one ? 'It goes' : 'They go'} to Recently deleted for 30 days.`)
  else if (trashable.length) body.push(`${plural(trashable.length, 'session')} will go to Recently deleted for 30 days.`)
  if (trashable.length < targets.length) body.push('Sessions with no transcript are simply stopped and forgotten; there is nothing to restore.')
  if (kept) body.push(one
    ? 'Keep running is on, so deleting also stops the running session. Undo restores it with Keep running off.'
    : kept === 1
      ? '1 has Keep running on, so deleting also stops it. Undo restores it with Keep running off.'
      : `${kept} have Keep running on, so deleting also stops them. Undo restores them with Keep running off.`)
  if (busy) body.push(one ? "It's working on a turn right now." : `${busy} are working on a turn right now.`)
  if (blocked.length) body.push(`${plural(blocked.length, 'session')} open, offline or list-only will be skipped.`)
  if (!await ctx.ui.confirm({
    title: one ? `Delete “${targets[0].title}”?` : `Delete ${targets.length} sessions?`,
    body, items: summaryItems(targets), confirmLabel: 'Delete', danger: true,
  })) return
  const restorable: string[] = []
  const done = await runBulk(ctx, 'Deleting', targets, async (id) => {
    const result = await ctx.post<{ trashed?: boolean }>(actionPath(id, 'delete'), {})
    if (result?.trashed !== false) restorable.push(id)
  })
  ctx.forget(done)
  if (!done.length) return
  const what = done.length === 1 ? `“${targets.find((row) => row.id === done[0])!.title}”` : plural(done.length, 'session')
  ctx.ui.toast(hasRemote(done)
    ? { message: 'Local deletes done; Mac deletes queued. Watch their action status.' }
    : { message: `Deleted ${what}.`, ...(restorable.length ? { action: { label: 'Undo', run: async () => { await restoreSessions(ctx, restorable.map((id) => targets.find((row) => row.id === id)!)) } } } : {}) })
}

/** Accept trash records rather than looking up deleted ids in the live catalogue. */
export async function restoreSessions(ctx: ActionContext, rows: readonly TrashSession[]): Promise<void> {
  const targets = rows.filter((row) => !hostLock({ ...row, host: row.host || '' }))
  if (!targets.length) {
    if (rows.length) ctx.ui.toast({ message: hostLock({ ...rows[0], host: rows[0].host || '' }) })
    return
  }
  const done = await runBulk(ctx, 'Restoring', targets, (id) => ctx.post(actionPath(id, 'restore'), {}))
  if (done.length) ctx.ui.toast({ message: hasRemote(done) ? 'Mac restores queued; watch their action status.' : `Restored ${plural(done.length, 'session')}.` })
}
