import { actionPath, hostLock } from '../hosts/hostRow'
import type { SessionRow } from '../api/sessionRow'
import type { ActionContext } from './context'

/** Single local actions retain the delayed refresh on success and refusal. */
export async function postLocalAction(ctx: ActionContext, id: string, action: string, body: object = {}): Promise<boolean> {
  try {
    await ctx.post(actionPath(id, action), body)
    return true
  } catch (error) {
    ctx.ui.toast({ message: `${action[0].toUpperCase() + action.slice(1)} failed: ${(error as Error).message}`, tone: 'error' })
    return false
  } finally {
    ctx.refreshAfter(600)
  }
}

type HostBodies = {
  rename: { title: string }
  move: { cwd: string }
  fork: { title: string; cwd: string }
  'fork-pi': { title: string }
  delete: Record<string, never>
}

/** Queue acknowledgement is not completion; pushed metadata owns the eventual result. */
export async function queueHostAction<K extends keyof HostBodies>(
  ctx: ActionContext, row: SessionRow, kind: K, body: HostBodies[K],
): Promise<boolean> {
  const locked = hostLock(row)
  if (locked) { ctx.ui.toast({ message: locked }); return false }
  try {
    await ctx.post(actionPath(row.id, kind), body)
    ctx.ui.toast({ message: `Queued ${kind} on ${row.host}; watch the action status for its result.` })
    return true
  } catch (error) {
    ctx.ui.toast({ message: `Couldn't queue ${kind}: ${(error as Error).message}`, tone: 'error' })
    return false
  } finally {
    await ctx.refresh()
  }
}
