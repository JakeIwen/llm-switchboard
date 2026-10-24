import type { SessionRow } from '../api/sessionRow'
import { sessionBlockedReason } from '../sessions/badges'
import { MAX_NAME_LENGTH } from './actionNames'
import { postLocalAction, queueHostAction } from './actionRequests'
import type { ActionContext } from './context'

export async function renameSession(ctx: ActionContext, row: SessionRow): Promise<void> {
  const blocked = sessionBlockedReason(row)
  if (blocked) { ctx.ui.toast({ message: blocked }); return }
  const entered = await ctx.ui.form({
    title: `Rename “${row.title}”`, confirmLabel: 'Rename',
    fields: [{ kind: 'text', key: 'title', label: 'Title', value: row.title, required: true, autofocus: true, maxLength: MAX_NAME_LENGTH }],
  })
  if (!entered) return
  const title = String(entered.title).trim()
  if (!title || (!row.host && title === row.title)) return
  if (row.host) { await queueHostAction(ctx, row, 'rename', { title }); return }
  if (await postLocalAction(ctx, row.id, 'rename', { title })) ctx.ui.toast({ message: `Renamed to “${title}”.` })
}
