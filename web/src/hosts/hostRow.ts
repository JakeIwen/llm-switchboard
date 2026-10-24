import type { HostAvailability, HostActionState, SessionRow } from '../api/sessionRow'
import type { Badge } from '../sessions/badges'

type HostRow = Pick<SessionRow, 'host' | 'host_online' | 'last_seen' | 'mutable' | 'state'>
export const ACTION_STATE: Record<HostActionState, string> = {
  queued: 'queued', sent: 'queued (delivered; awaiting result)', done: 'done', failed: 'failed', expired: 'expired',
}
const seenTime = (time: number | null | undefined): string =>
  new Date((time as number) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
export const hostStatus = (host: { online?: boolean | null; last_seen?: number | null }): string =>
  host.online ? 'online' : `offline, last seen ${seenTime(host.last_seen)}`
export const hostSummary = (hosts: readonly HostAvailability[] = []): string =>
  hosts.map((host) => `${host.host}: ${hostStatus(host)}`).join(' · ')
export const hostBadge = (session: Pick<SessionRow, 'host' | 'host_online' | 'last_seen'>): Badge => ({
  label: `${session.host} · ${hostStatus({ online: session.host_online, last_seen: session.last_seen })}`,
  tone: session.host_online ? 'info' : 'idle',
})

export function hostLock(session: HostRow): string {
  if (!session.host) return ''
  if (!session.host_online) return 'Mac is offline; wait for its next push.'
  if (!session.mutable) return 'Mac Codex is list-only until its app-server daemon is available.'
  if (session.state !== 'closed') return 'Open or working on the Mac; close it before changing it.'
  return ''
}

export function actionPath(id: string, action: string): string {
  const colon = id.indexOf(':')
  return colon < 0 ? `/api/sessions/${id}/${action}`
    : `/api/hosts/${id.slice(0, colon)}/sessions/${id.slice(colon + 1)}/${action}`
}
export const hasRemote = (ids: readonly string[]): boolean => ids.some((id) => id.includes(':'))

export interface HostAction {
  host: string
  title: string
  action: string
  state: HostActionState
  queued_at: number
  message?: string
}
const RECENT_S = 600
/** Recent action lines, in the same newest-first order as the old panel. */
export const actionSummary = (actions: readonly HostAction[] = [], now: number): string[] =>
  actions.filter((action) => action.queued_at > now - RECENT_S).slice(-20).reverse().map((action) =>
    `${action.host} · ${action.title} · ${action.action}: ${ACTION_STATE[action.state] || action.state}${action.message ? ' — ' + action.message : ''}`)
