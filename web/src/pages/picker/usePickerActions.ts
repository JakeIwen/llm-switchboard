import { useMemo } from 'react'
import type { ActionUi } from '../../actions/ui'
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

/** The page's buttons, wired to the action flows in actions/. */
export function usePickerActions({ ui }: Deps) {
  return useMemo(() => {
    const pending = (what: string) => () => ui.toast({ message: `${what}: not wired yet`, tone: 'error' })
    const copied = (ok: boolean) => ui.toast({ message: ok ? 'Copied.' : "Couldn't copy; select the text instead." })
    const row: RowActions = {
      rename: pending('Rename'),
      remoteControlName: pending('Remote Control name'),
      move: pending('Move'),
      fork: pending('Fork'),
      forkToPi: pending('Fork onto the Pi'),
      restart: pending('Restart'),
      remove: pending('Delete'),
      copied,
    }
    return {
      row,
      copied,
      keep: (_row: SessionRow, _keep: boolean) => pending('Keep running')(),
      bulk: (action: BulkAction, _ids: string[]) => pending(`Bulk ${action}`)(),
      newSession: pending('New session'),
      settings: pending('Settings'),
      restoreTrash: (_item: TrashItem) => pending('Restore')(),
      emptyTrash: pending('Empty trash'),
    }
  }, [ui])
}
