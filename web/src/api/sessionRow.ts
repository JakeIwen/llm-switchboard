// Mirrors catalog.py, supervisor_state.py and hosts.page_row in GET /api/sessions.
// Generated OpenAPI types will replace these hand-maintained records.
export const TOOLS = { claude: 'claude', codex: 'codex' } as const
export type SessionTool = keyof typeof TOOLS
export type SupervisorState = 'running' | 'starting' | 'attention' | 'elsewhere' | 'offline' | 'backoff' | 'missing' | 'error'
export type CodexState = 'notLoaded' | 'idle' | 'active' | 'systemError'
export type HostSessionState = 'closed' | 'idle' | 'busy'
export type HostActionState = 'queued' | 'sent' | 'done' | 'failed' | 'expired'

export interface SessionExit {
  status: number | null
  at: number
  ran_s: number
}

export interface SessionStatus {
  session_id: string
  state: SupervisorState
  detail: string
  window_id: string | null
  pid: number | null
  rss_kb: number | null
  rc_active: boolean
  busy: boolean
  duplicate: boolean
  started_at: number | null
  next_attempt: number
  failures: number
  launches: number
  last_exit: SessionExit | null
  rc_name: string
}

// Local Claude processes have a pid; remote metadata deliberately omits it.
export interface OpenProcess {
  pid?: number
  status: string
}

export interface HostAvailability {
  host: string
  online: boolean
  last_seen: number
}

export interface SessionRow {
  id: string
  tool: SessionTool
  host: string
  short_id: string
  title: string
  // Host rows never include prompts.
  first_prompt?: string
  cwd: string
  project: string
  mtime: number | null
  size: number | null
  remote_url: string
  resume_cmd: string
  new: boolean
  enabled: boolean
  rc_name: string
  open_elsewhere: OpenProcess[]
  status: SessionStatus | null
  // Present for local Codex and host rows, absent for local Claude.
  codex_state?: CodexState
  // Host-only fields; id is host:session_id, while session_id is the raw id.
  session_id?: string
  host_online?: boolean
  last_seen?: number
  state?: HostSessionState
  mutable?: boolean
}
