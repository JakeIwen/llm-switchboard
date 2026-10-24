import { describe, expect, it } from 'vitest'
import type { SessionRow, SessionStatus } from '../api/sessionRow'
import { badges, identityTags, sessionBlockedReason, STATE } from './badges'

type BadgeRow = Parameters<typeof badges>[0] & Parameters<typeof sessionBlockedReason>[0]
const base: BadgeRow = { host: '', enabled: false, status: null, new: false, tool: 'claude', open_elsewhere: [] }
const status = (fields: Partial<SessionStatus>): SessionStatus => ({
  session_id: 'a', state: 'running', detail: '', window_id: null, pid: null, rss_kb: null,
  rc_active: false, busy: false, duplicate: false, started_at: null, next_attempt: 0,
  failures: 0, launches: 0, last_exit: null, rc_name: '', ...fields,
})

describe('session badges', () => {
  it('retains the running label and memory badge from session cards', () => {
    expect(badges({ ...base, enabled: true, status: status({ state: 'running', rss_kb: 2048 }) })).toEqual([
      { label: 'Remote Control on', tone: 'ok' }, { label: '2 MB', tone: 'idle' },
    ])
    expect(identityTags(base)).toEqual([])
  })
  it('mirrors every supervisor state', () => {
    const expected = [
      ['running', 'Remote Control on', 'ok'], ['starting', 'Starting', 'info'],
      ['attention', 'Needs attention', 'warn'], ['elsewhere', 'Waiting: open elsewhere', 'idle'],
      ['offline', 'Waiting for network', 'idle'], ['backoff', 'Exited, relaunching', 'warn'],
      ['missing', 'Transcript missing', 'bad'], ['error', 'Error', 'bad'],
    ] as const
    for (const [state, label, tone] of expected) {
      expect(STATE[state]).toEqual({ label, tone })
      expect(badges({ ...base, enabled: true, status: status({ state }) })).toEqual([{ label, tone }])
    }
  })
  it('keeps queued/new/working/open badges in their original order', () => {
    expect(badges({ ...base, enabled: true, new: true, tool: 'codex', codex_state: 'active', open_elsewhere: [{ pid: 42, status: 'busy' }] }))
      .toEqual([{ label: 'Queued', tone: 'info' }, { label: 'New', tone: 'info' },
        { label: 'Working', tone: 'info' }, { label: 'Open in a terminal', tone: 'idle' }])
    expect(badges({ ...base, enabled: true, new: true, status: status({ busy: true, rss_kb: 1536 }) }))
      .toEqual([{ label: 'Remote Control on', tone: 'ok' }, { label: 'Working', tone: 'info' },
        { label: '2 MB', tone: 'idle' }, { label: 'New', tone: 'info' }])
  })
  it('suppresses only the redundant elsewhere terminal badge', () => {
    const open_elsewhere = [{ pid: 42, status: 'idle' }]
    expect(badges({ ...base, enabled: true, status: status({ state: 'elsewhere' }), open_elsewhere }))
      .toEqual([{ label: 'Waiting: open elsewhere', tone: 'idle' }])
    expect(badges({ ...base, status: status({ state: 'elsewhere', busy: true }), open_elsewhere }))
      .toEqual([{ label: 'Open in a terminal', tone: 'idle' }])
  })
  it('omits zero memory and disabled supervisor status, with a fallback for unknown states', () => {
    expect(badges(base)).toEqual([])
    expect(badges({ ...base, status: status({ busy: true, rss_kb: 2048 }) })).toEqual([])
    expect(badges({ ...base, enabled: true, status: status({ rss_kb: 0 }) })).toEqual([{ label: 'Remote Control on', tone: 'ok' }])
    expect(badges({ ...base, enabled: true, status: status({ state: 'future' as SessionStatus['state'] }) }))
      .toEqual([{ label: 'future', tone: 'idle' }])
    expect(badges({ ...base, codex_state: 'active' })).toEqual([])
  })
  it('puts the host availability first and does not merge duplicate working badges', () => {
    expect(badges({ ...base, host: 'mac', host_online: true, new: true }))
      .toEqual([{ label: 'mac · online', tone: 'info' }, { label: 'New', tone: 'info' }])
    expect(badges({ ...base, enabled: true, tool: 'codex', codex_state: 'active', status: status({ busy: true }) }))
      .toEqual([{ label: 'Remote Control on', tone: 'ok' }, { label: 'Working', tone: 'info' }, { label: 'Working', tone: 'info' }])
  })
})

describe('identity and blocked reasons', () => {
  it('pairs machine and tool tags on cards without a keep switch', () => {
    const cases: [Pick<SessionRow, 'host' | 'tool'>, { title: string; label: string }[]][] = [
      [{ host: '', tool: 'codex' }, [{ title: 'A Codex session', label: 'Codex' }, { title: 'This Pi', label: 'Pi' }]],
      [{ host: 'mac<&"', tool: 'claude' }, [{ title: 'mac<&"', label: 'Mac' }, { title: 'A Claude Code session', label: 'Claude' }]],
      [{ host: 'mac', tool: 'codex' }, [{ title: 'mac', label: 'Mac' }, { title: 'A Codex session', label: 'Codex' }]],
    ]
    for (const [row, expected] of cases) expect(identityTags(row)).toEqual(expected)
  })
  it('reports the first local pid, with host locks taking priority', () => {
    const open_elsewhere = [{ pid: 42, status: 'idle' }, { pid: 43, status: 'busy' }]
    expect(sessionBlockedReason(base)).toBe('')
    expect(sessionBlockedReason({ ...base, open_elsewhere })).toBe('Open in another claude process (pid 42); close it to rename, move or delete.')
    expect(sessionBlockedReason({ ...base, host: 'mac', host_online: false, open_elsewhere })).toBe('Mac is offline; wait for its next push.')
    expect(sessionBlockedReason({ ...base, host: 'mac', host_online: true, mutable: true, state: 'closed' })).toBe('')
  })
  it('matches the old card badge data across all supervisor and optional branches', async () => {
    const url = new URL('../../../llm_switchboard/static/session-cards.js', import.meta.url).href
    const legacy = await import(url)
    const selection = { has: () => false, cursor: null }
    const cards = legacy.createSessionCards(selection, new Set(), new Map(), new Map(), (row: SessionRow) => row.project, () => null)
    const rows: BadgeRow[] = [base, { ...base, enabled: true }, { ...base, new: true },
      { ...base, host: 'mac', host_online: true }, { ...base, host: 'mac', host_online: false, last_seen: 100 },
      { ...base, tool: 'codex', codex_state: 'active' }]
    for (const state of Object.keys(STATE) as SessionStatus['state'][]) {
      for (const enabled of [true, false]) {
        rows.push({ ...base, enabled, status: status({ state, busy: true, rss_kb: 2048 }),
          open_elsewhere: [{ pid: 42, status: 'idle' }] })
      }
    }
    for (const row of rows) {
      const fullRow = { id: 'a', title: 'Alpha', short_id: 'a', cwd: '/work/project', project: '~/project',
        mtime: 0, size: 12, rc_name: '', ...row }
      const html: string = cards.render(fullRow)
      const expected = [...html.matchAll(/<span class="badge b-([a-z]+)">([^<]*)<\/span>/g)]
        .map((match) => ({ tone: match[1], label: match[2] }))
      expect(badges(row)).toEqual(expected)
      expect(sessionBlockedReason(row)).toBe(legacy.sessionBlockedReason(row))
    }
  })
})
