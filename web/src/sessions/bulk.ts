import type { SessionRow } from '../api/sessionRow'
import { plural } from '../format/format'

export function splitBlocked<T extends Pick<SessionRow, 'id'>>(
  ids: readonly string[], lookup: (id: string) => T | null | undefined,
  blockedReason: (session: NoInfer<T>) => string,
): { targets: T[]; blocked: T[] } {
  const sessions = ids.map(lookup).filter((session): session is T => Boolean(session))
  return {
    targets: sessions.filter((session) => !blockedReason(session)),
    blocked: sessions.filter((session) => blockedReason(session)),
  }
}

interface BulkDependencies {
  lookup: (id: string) => Pick<SessionRow, 'title'> | null | undefined
  showProgress: (value: string) => void
  refresh: () => Promise<unknown>
  // Rendering the failure dialog belongs to the caller, not this model.
  reportFailures: (failure: { title: string; body: string; confirm: string }) => Promise<unknown>
}
export class BulkActions {
  constructor(private readonly dependencies: BulkDependencies) {}

  async run(verb: string, ids: readonly string[], action: (id: string) => Promise<unknown>): Promise<string[]> {
    const done: string[] = [], failed: string[] = []
    const { lookup, showProgress, refresh, reportFailures } = this.dependencies
    for (const [index, id] of ids.entries()) {
      showProgress(ids.length > 1 ? `${verb} ${index + 1} of ${ids.length}…` : `${verb}…`)
      try { await action(id); done.push(id) }
      catch (error) { failed.push(`${lookup(id)?.title || id}: ${(error as Error).message}`) }
    }
    showProgress('')
    await refresh()
    if (failed.length) {
      await reportFailures({ title: `${plural(failed.length, 'session')} failed`, body: failed.join('\n'), confirm: 'OK' })
    }
    return done
  }
}
