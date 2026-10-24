import { TOOLS, type SessionRow } from '../api/sessionRow'
import { MAX_NAME_LENGTH } from './actionNames'
import { postLocalAction } from './actionRequests'
import type { ActionContext } from './context'

export async function remoteControlName(ctx: ActionContext, row: SessionRow): Promise<void> {
  if (row.host) { ctx.ui.toast({ message: 'This action is unavailable on the Mac.' }); return }
  if (row.tool === TOOLS.codex) return
  const entered = await ctx.ui.form({
    title: `Remote Control name for “${row.title}”`, confirmLabel: 'Save',
    fields: [{ kind: 'text', key: 'name', label: 'Remote Control name (blank = session title; applies on next launch)',
      value: row.rc_name, placeholder: row.status?.rc_name || row.title, autofocus: true, maxLength: MAX_NAME_LENGTH }],
  })
  if (!entered) return
  await postLocalAction(ctx, row.id, 'name', { name: String(entered.name) })
}
