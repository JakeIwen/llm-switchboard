import { TOOLS, type SessionRow } from '../api/sessionRow'
import { postLocalAction } from './actionRequests'
import type { ActionContext } from './context'

export async function restartSession(ctx: ActionContext, row: SessionRow): Promise<void> {
  if (row.host) { ctx.ui.toast({ message: 'This action is unavailable on the Mac.' }); return }
  if (row.tool === TOOLS.codex || !row.enabled) return
  if (row.status?.busy && !await ctx.ui.confirm({
    title: 'Restart it?', body: ["It's working on a turn right now."], confirmLabel: 'Restart', danger: true,
  })) return
  await postLocalAction(ctx, row.id, 'restart')
}
