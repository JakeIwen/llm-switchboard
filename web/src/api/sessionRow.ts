// Placeholder until the ported helpers and generated OpenAPI types land; mirrors
// catalog.py's rows as `GET /api/sessions` returns them.

export type Tool = 'claude' | 'codex'

export interface SupervisorStatus {
  session_id: string
  state: string
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
  last_exit: number | null
  rc_name: string
}

export interface OpenElsewhere {
  pid: number
  status: string
}

export interface SessionRow {
  id: string
  tool: Tool
  host: string
  short_id: string
  title: string
  first_prompt: string
  cwd: string
  project: string
  mtime: number
  size: number
  remote_url: string
  resume_cmd: string
  new: boolean
  enabled: boolean
  rc_name: string
  open_elsewhere: OpenElsewhere[]
  status: SupervisorStatus | null
  state?: string
  mutable?: boolean
  session_id?: string
  host_online?: boolean
  last_seen?: number
  codex_state?: string
}
