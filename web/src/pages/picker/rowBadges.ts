import type { SessionRow } from '../../api/sessionRow'

// Placeholder for the ported badges module (sessions/badges.ts).
export type Tone = 'ok' | 'info' | 'warn' | 'bad' | 'idle'

export const toneClass: Record<Tone, string> = {
  ok: 'tone-ok',
  info: 'tone-info',
  warn: 'tone-warn',
  bad: 'tone-bad',
  idle: 'tone-idle',
}

const STATE: Record<string, [string, Tone]> = {
  running: ['Remote Control on', 'ok'],
  starting: ['Starting', 'info'],
  attention: ['Needs attention', 'warn'],
  elsewhere: ['Waiting: open elsewhere', 'idle'],
  offline: ['Waiting for network', 'idle'],
  backoff: ['Exited, relaunching', 'warn'],
  missing: ['Transcript missing', 'bad'],
  error: ['Error', 'bad'],
}

export function rowBadges(row: SessionRow): { label: string; tone: Tone }[] {
  const out: { label: string; tone: Tone }[] = []
  const status = row.status
  if (row.enabled && status) {
    const [label, tone] = STATE[status.state] ?? [status.state, 'idle']
    out.push({ label, tone })
    if (status.busy) out.push({ label: 'Working', tone: 'info' })
    if (status.rss_kb) out.push({ label: `${Math.round(status.rss_kb / 1024)} MB RAM`, tone: 'idle' })
  } else if (row.enabled) {
    out.push({ label: 'Queued', tone: 'info' })
  }
  if (row.new) out.push({ label: 'New', tone: 'info' })
  if (row.tool === 'codex' && row.codex_state === 'active') out.push({ label: 'Working', tone: 'info' })
  if (row.open_elsewhere.length && !(row.enabled && status?.state === 'elsewhere')) {
    out.push({ label: 'Open in a terminal', tone: 'idle' })
  }
  if (row.host && row.host_online === false) out.push({ label: 'Mac offline', tone: 'idle' })
  return out
}
