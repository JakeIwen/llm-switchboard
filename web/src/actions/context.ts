import type { getJson, postJson } from '../api/client'
import type { SessionRow } from '../api/sessionRow'
import type { SessionsPayload } from '../api/sessionsPayload'
import type { ActionUi } from './ui'

/** The page supplies live snapshots, not a catalogue captured when a button rendered. */
export interface ActionContext {
  ui: ActionUi
  post: typeof postJson
  get: typeof getJson
  refresh(): Promise<void>
  /** Schedule a refresh without holding the form/action open (the legacy single-action delay is 600ms). */
  refreshAfter(delayMs: number): void
  rows(): readonly SessionRow[]
  payload(): Pick<SessionsPayload, 'notifications' | 'codex'> | null
  knownFolders(): readonly string[]
  forget(ids: readonly string[]): void
  /** Move the cursor and scroll to the row, without expanding or selecting it. */
  focus(id: string): void
  /** Expand, move the cursor and scroll to a row (the Details toast action). */
  reveal(id: string): void
  /** Owned by the page; call checkClaudeReadiness after each successful catalogue refresh. */
  watching: Map<string, string>
}

export interface CreatedSession { id: string; title: string }

/** The action-relevant projection of GET /api/trash; local items omit host fields. */
/** A Recently deleted entry: a Pi entry, or a Mac session row (GET /api/trash). */
export type TrashSession = Pick<SessionRow, 'id' | 'title'> & Partial<Pick<SessionRow, 'host' | 'host_online' | 'mutable' | 'state'>>
