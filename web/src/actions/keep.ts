import { TOOLS, type SessionRow } from '../api/sessionRow'
import { plural } from '../format/format'
import { actionPath } from '../hosts/hostRow'
import { runBulk, summaryItems } from './actionBatch'
import { postLocalAction } from './actionRequests'
import type { ActionContext } from './context'

/** False tells a controlled switch to retain its previous checked state. */
export async function toggleKept(ctx: ActionContext, row: SessionRow, on: boolean): Promise<boolean> {
  if (row.host) { ctx.ui.toast({ message: 'This action is unavailable on the Mac.' }); return false }
  if (row.tool === TOOLS.codex) {
    ctx.ui.toast({ message: 'Codex sessions have no keep-running; the Codex daemon holds them all.' })
    return false
  }
  if (on === row.enabled) return false
  if (!on && row.status?.busy && !await ctx.ui.confirm({
    title: 'Stop keeping it running?', body: ["It's working on a turn right now."], confirmLabel: 'Stop', danger: true,
  })) return false
  return postLocalAction(ctx, row.id, on ? 'enable' : 'disable')
}

export async function setKept(ctx: ActionContext, ids: readonly string[], on: boolean): Promise<void> {
  const rows = ids.map((id) => ctx.rows().find((row) => row.id === id)).filter((row): row is SessionRow => Boolean(row))
  const skipped = rows.filter((row) => row.host).length
  const local = rows.filter((row) => !row.host)
  if (!rows.length) return
  if (!local.length) {
    ctx.ui.toast({ message: `${skipped} Mac sessions skipped: the Pi cannot keep them running.` }); return
  }
  const targets = local.filter((row) => row.tool !== TOOLS.codex && row.enabled !== on)
  if (!targets.length) {
    const message = skipped ? `${skipped} Mac sessions skipped; no selected Pi Claude session needs this change.`
      : local.every((row) => row.tool === TOOLS.codex) ? 'Codex sessions have no keep-running; the Codex daemon holds them all.'
        : on ? 'Already kept running.' : 'None of these are kept running.'
    ctx.ui.toast({ message }); return
  }
  const busy = targets.filter((row) => row.status?.busy).length
  if (on && targets.length > 2 && !await ctx.ui.confirm({
    title: `Keep ${targets.length} sessions running?`, items: summaryItems(targets),
    body: ['Each is a claude process using roughly 300 MB.'], confirmLabel: 'Keep running',
  })) return
  if (!on && busy && !await ctx.ui.confirm({
    title: `Stop ${plural(targets.length, 'session')}?`, body: [`${plural(busy, 'session')} working on a turn right now.`],
    confirmLabel: 'Stop', danger: true,
  })) return
  const done = await runBulk(ctx, on ? 'Enabling' : 'Stopping', targets,
    (id) => ctx.post(actionPath(id, on ? 'enable' : 'disable'), {}))
  if (done.length) ctx.ui.toast({
    message: `${on ? 'Keeping' : 'Stopped keeping'} ${plural(done.length, 'session')}${on ? ' running' : ''}.${skipped ? ` Skipped ${skipped} Mac sessions.` : ''}`,
  })
}
