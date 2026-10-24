import type { SessionRow } from '../api/sessionRow'
import { BulkActions } from '../sessions/bulk'
import type { ActionContext } from './context'

export function summaryItems(rows: readonly Pick<SessionRow, 'title'>[]): string[] {
  return rows.length < 2 ? [] : [
    ...rows.slice(0, 8).map((row) => row.title),
    ...(rows.length > 8 ? [`…and ${rows.length - 8} more`] : []),
  ]
}

export function runBulk(
  ctx: ActionContext, verb: string, targets: readonly Pick<SessionRow, 'id' | 'title'>[],
  action: (id: string) => Promise<unknown>,
): Promise<string[]> {
  const lookup = new Map(targets.map((row) => [row.id, row]))
  return new BulkActions({
    lookup: (id) => lookup.get(id),
    showProgress: (text) => ctx.ui.progress(text || null),
    refresh: () => ctx.refresh(),
    reportFailures: ({ title, body }) => ctx.ui.alert({ title, body: [body] }),
  }).run(verb, targets.map((row) => row.id), action)
}
