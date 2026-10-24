import type { SessionRow } from './sessionRow'

export interface HostStatus {
  host: string
  online: boolean
  last_seen: number
}

export interface HostAction {
  id: string
  session_id: string
  tool: string
  action: string
  title: string
  cwd: string
  queued_at: number
  state: string
  message: string
  host: string
}

export interface FolderSettings {
  depth: number
  min_sessions: number
  hidden: string[]
}

export interface InstallSettings {
  claude_command: string
  codex_command: string
  folders: FolderSettings
  poll_seconds: number
}

/** `GET /api/sessions` */
export interface SessionsPayload {
  host: string
  now: number
  sessions: SessionRow[]
  notifications: boolean
  codex: { enabled: boolean; error: string }
  hosts: HostStatus[]
  host_actions: HostAction[]
  settings: InstallSettings
}

/** One `GET /api/trash` entry. Mac entries also carry the row's host fields. */
export interface TrashItem {
  id: string
  tool: 'claude' | 'codex'
  short_id: string
  title: string
  project: string
  deleted_at: number
  host?: string
  host_online?: boolean
  last_seen?: number
  mutable?: boolean
  state?: 'closed' | 'idle' | 'busy'
}
