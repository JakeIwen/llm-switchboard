import { plural } from '../format/format'
import type { ActionContext, TrashSession } from './context'

export async function emptyPiTrash(ctx: ActionContext, rows: readonly TrashSession[]): Promise<void> {
  const count = rows.filter((row) => !row.host).length
  if (!count) return
  if (!await ctx.ui.confirm({
    title: `Permanently delete ${plural(count, 'Pi session')}?`,
    body: ["This clears only the Pi trash and can't be undone. Mac trash is managed on the Mac."],
    confirmLabel: 'Delete forever', danger: true,
  })) return
  try {
    await ctx.post('/api/trash/empty', {})
    ctx.ui.toast({ message: 'Pi trash emptied. Mac trash stays on the Mac.' })
  } catch (error) { ctx.ui.toast({ message: `Couldn't empty the trash: ${(error as Error).message}`, tone: 'error' }) }
  await ctx.refresh()
}
