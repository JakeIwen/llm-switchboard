import { TOOLS, type SessionRow, type SupervisorState } from '../api/sessionRow'
import { hostBadge, hostLock } from '../hosts/hostRow'

export type BadgeTone = 'ok' | 'info' | 'warn' | 'idle' | 'bad'
export interface Badge { label: string; tone: BadgeTone }
export const STATE: Record<SupervisorState, Badge> = {
  running: { label: 'Remote Control on', tone: 'ok' },
  starting: { label: 'Starting', tone: 'info' },
  attention: { label: 'Needs attention', tone: 'warn' },
  elsewhere: { label: 'Waiting: open elsewhere', tone: 'idle' },
  offline: { label: 'Waiting for network', tone: 'idle' },
  backoff: { label: 'Exited, relaunching', tone: 'warn' },
  missing: { label: 'Transcript missing', tone: 'bad' },
  error: { label: 'Error', tone: 'bad' },
}

type BadgeRow = Pick<SessionRow, 'host' | 'host_online' | 'last_seen' | 'enabled' | 'status' | 'new' | 'tool' | 'codex_state' | 'open_elsewhere'>
export function sessionBlockedReason(session: Pick<SessionRow, 'host' | 'host_online' | 'mutable' | 'state' | 'open_elsewhere'>): string {
  return hostLock(session) || (session.open_elsewhere.length
    ? `Open in another claude process (pid ${session.open_elsewhere[0].pid}); close it to rename, move or delete.` : '')
}

export function badges(session: BadgeRow): Badge[] {
  const out = session.host ? [hostBadge(session)] : []
  const status = session.status
  if (session.enabled && status) {
    out.push(STATE[status.state] || { label: status.state, tone: 'idle' })
    if (status.busy) out.push({ label: 'Working', tone: 'info' })
    if (status.rss_kb) out.push({ label: `${Math.round(status.rss_kb / 1024)} MB`, tone: 'idle' })
  } else if (session.enabled) {
    out.push({ label: 'Queued', tone: 'info' })
  }
  if (session.new) out.push({ label: 'New', tone: 'info' })
  if (session.tool === TOOLS.codex && session.codex_state === 'active') out.push({ label: 'Working', tone: 'info' })
  if (session.open_elsewhere.length && !(session.enabled && status && status.state === 'elsewhere')) {
    out.push({ label: 'Open in a terminal', tone: 'idle' })
  }
  return out
}

const TOOL_IDENTITY: Record<SessionRow['tool'], { label: string; title: string }> = {
  claude: { label: 'Claude', title: 'A Claude Code session' },
  codex: { label: 'Codex', title: 'A Codex session' },
}
export interface IdentityTag { label: string; title: string }
/** Local Claude keeps its switch; other cards put the remote machine before the tool. */
export function identityTags(session: Pick<SessionRow, 'host' | 'tool'>): IdentityTag[] {
  if (!session.host && session.tool !== TOOLS.codex) return []
  const tool = TOOL_IDENTITY[session.tool]
  const machine = { title: session.host || 'This Pi', label: session.host ? 'Mac' : 'Pi' }
  return session.host ? [machine, tool] : [tool, machine]
}
