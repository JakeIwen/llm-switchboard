import { useEffect, useMemo, useRef } from 'react'
import type { ActionContext } from '../../actions/context'
import { deleteSession, deleteSessions, restoreSessions } from '../../actions/deleteRestore'
import { forkOntoPi, forkSession } from '../../actions/fork'
import { setKept, toggleKept } from '../../actions/keep'
import { moveSession, moveSessions } from '../../actions/move'
import { newSession } from '../../actions/newSession'
import { checkClaudeReadiness } from '../../actions/readiness'
import { remoteControlName } from '../../actions/remoteControlName'
import { renameSession } from '../../actions/rename'
import { restartSession } from '../../actions/restart'
import { openSettings } from '../../actions/settings'
import { emptyPiTrash } from '../../actions/trash'
import type { ActionUi } from '../../actions/ui'
import { getJson, postJson } from '../../api/client'
import type { SessionRow } from '../../api/sessionRow'
import type { TrashItem } from '../../api/sessionsPayload'
import type { BulkAction } from './BulkBar'
import type { RowActions } from './SessionDetails'
import type { usePickerFilters } from './usePickerFilters'
import type { useSelection } from './useSelection'
import type { SessionsState } from './useSessions'

interface Deps {
  ui: ActionUi
  sessions: SessionsState
  filters: ReturnType<typeof usePickerFilters>
  selection: ReturnType<typeof useSelection>
  reveal: (id: string) => void
  forget: (ids: Iterable<string>) => void
}

const scrollTo = (id: string) =>
  requestAnimationFrame(() => document.querySelector(`[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest' }))

/** The page's buttons, wired to the action flows in actions/. The flows read
    live snapshots through refs, so the context stays one stable object. */
export function usePickerActions(deps: Deps) {
  const latest = useRef(deps)
  latest.current = deps
  const watching = useRef(new Map<string, string>())

  const ctx = useMemo<ActionContext>(
    () => ({
      ui: deps.ui,
      post: postJson,
      get: getJson,
      refresh: () => latest.current.sessions.refresh(),
      refreshAfter: (ms) => void window.setTimeout(() => void latest.current.sessions.refresh(), ms),
      rows: () => latest.current.sessions.data?.sessions ?? [],
      payload: () => latest.current.sessions.data,
      knownFolders: () =>
        [...new Set((latest.current.sessions.data?.sessions ?? []).map((r) => r.cwd).filter(Boolean))].sort(),
      forget: (ids) => latest.current.forget(ids),
      focus: (id) => {
        latest.current.selection.moveCursor(id)
        scrollTo(id)
      },
      reveal: (id) => latest.current.reveal(id),
      watching: watching.current,
    }),
    [deps.ui],
  )

  // New and forked kept sessions are watched until they come up or need help.
  const data = deps.sessions.data
  useEffect(() => {
    if (data) checkClaudeReadiness(ctx)
  }, [data, ctx])

  return useMemo(() => {
    const copied = (ok: boolean) => ctx.ui.toast({ message: ok ? 'Copied.' : "Couldn't copy; select the text instead." })
    const row: RowActions = {
      rename: (r) => void renameSession(ctx, r),
      remoteControlName: (r) => void remoteControlName(ctx, r),
      move: (r) => void moveSession(ctx, r),
      fork: (r) => void forkSession(ctx, r),
      forkToPi: (r) => void forkOntoPi(ctx, r),
      restart: (r) => void restartSession(ctx, r),
      remove: (r) => void deleteSession(ctx, r),
      copied,
    }
    const BULK: Record<BulkAction, (ids: string[]) => void> = {
      keep: (ids) => void setKept(ctx, ids, true),
      stop: (ids) => void setKept(ctx, ids, false),
      move: (ids) => void moveSessions(ctx, ids),
      delete: (ids) => void deleteSessions(ctx, ids),
      all: () => latest.current.selection.selectAll(),
      clear: () => latest.current.selection.clear(),
    }
    return {
      row,
      copied,
      keep: (r: SessionRow, on: boolean) => toggleKept(ctx, r, on),
      bulk: (action: BulkAction, ids: string[]) => BULK[action](ids),
      newSession: () => {
        const { filters } = latest.current
        // Start where the list is looking: the one picked Pi folder, the chosen tool.
        const only = filters.folders.length === 1 && !filters.folders[0].includes(':') ? filters.folders[0] : undefined
        void newSession(ctx, { tool: filters.tool === 'all' ? undefined : filters.tool, preferredFolder: only })
      },
      settings: () => void openSettings(ctx),
      restoreTrash: (item: TrashItem) => void restoreSessions(ctx, [item]),
      emptyTrash: () => void emptyPiTrash(ctx, latest.current.sessions.trash),
    }
  }, [ctx])
}
