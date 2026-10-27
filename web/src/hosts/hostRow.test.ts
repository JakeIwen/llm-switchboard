import { describe, expect, it } from 'vitest'
import { ACTION_STATE, actionPath, actionSummary, hasRemote, hostBadge, hostLock, hostStatus, hostSummary, type HostAction } from './hostRow'

describe('host availability', () => {
  it('keeps machine and availability without repeating the tool', () => {
    for (const tool of ['claude', 'codex']) {
      const row = { host: 'mac', host_online: true, tool }
      expect(hostBadge(row)).toEqual({ label: 'mac · online', tone: 'info' })
      const offline = hostBadge({ ...row, host_online: false, last_seen: 100 })
      expect(offline.tone).toBe('idle')
      expect(offline.label).toMatch(/^mac · offline, last seen [^<·]+$/)
      expect(offline.label).toBe(`mac · offline, last seen ${new Date(100000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}`)
    }
  })
  it('summarizes hosts in their provided order and permits an empty list', () => {
    expect(hostStatus({ online: true, last_seen: 0 })).toBe('online')
    const hosts = [{ host: 'mac', online: true, last_seen: 100, search_url: '' }, { host: 'other', online: false, last_seen: 100, search_url: '' }]
    expect(hostSummary(hosts)).toBe(`mac: online · other: ${hostStatus(hosts[1])}`)
    expect(hostSummary()).toBe('')
  })
  it('preserves lock priority and all refusal messages', () => {
    const base = { host: 'mac', host_online: true, mutable: true, state: 'closed' } as const
    expect(hostLock({ ...base, host: '', host_online: false, mutable: false })).toBe('')
    expect(hostLock({ ...base, host_online: false, mutable: false, state: 'busy' })).toBe('Mac is offline; wait for its next push.')
    expect(hostLock({ ...base, mutable: false, state: 'busy' })).toBe('Mac Codex is list-only until its app-server daemon is available.')
    for (const state of ['idle', 'busy', undefined] as const) {
      expect(hostLock({ ...base, state })).toBe('Open or working on the Mac; close it before changing it.')
    }
    expect(hostLock(base)).toBe('')
  })
})

describe('host routing and action status', () => {
  it('routes local and host ids using the first colon only', () => {
    expect(actionPath('abc', 'rename')).toBe('/api/sessions/abc/rename')
    expect(actionPath('mac:abc', 'fork-pi')).toBe('/api/hosts/mac/sessions/abc/fork-pi')
    expect(actionPath('mac:abc:def', 'delete')).toBe('/api/hosts/mac/sessions/abc:def/delete')
    expect(hasRemote([])).toBe(false)
    expect(hasRemote(['abc', 'def'])).toBe(false)
    expect(hasRemote(['abc', 'mac:def'])).toBe(true)
  })
  it('maps delivered actions to visibly queued, and preserves optional messages', () => {
    expect(ACTION_STATE).toEqual({ queued: 'queued', sent: 'queued (delivered; awaiting result)', done: 'done', failed: 'failed', expired: 'expired' })
    const actions: HostAction[] = Object.keys(ACTION_STATE).map((state) => ({
      host: 'mac', title: 'Alpha', action: 'rename', state: state as HostAction['state'], queued_at: 1000,
    }))
    expect(actionSummary(actions, 1000)).toEqual([
      'mac · Alpha · rename: expired', 'mac · Alpha · rename: failed', 'mac · Alpha · rename: done',
      'mac · Alpha · rename: queued (delivered; awaiting result)', 'mac · Alpha · rename: queued',
    ])
    expect(actionSummary([{ ...actions[0], state: 'future' as HostAction['state'], message: 'reason' }], 1000))
      .toEqual(['mac · Alpha · rename: future — reason'])
    expect(actionSummary(undefined, 1000)).toEqual([])
  })
  it('keeps only the last twenty recent actions, with a strict ten-minute cutoff', () => {
    const actions: HostAction[] = Array.from({ length: 25 }, (_, i) => ({ host: 'mac', title: `${i}`, action: 'rename', state: 'done', queued_at: 1000 }))
    actions.unshift({ ...actions[0], title: 'old', queued_at: 400 })
    const summary = actionSummary(actions, 1000)
    expect(summary).toHaveLength(20)
    expect(summary[0]).toBe('mac · 24 · rename: done')
    expect(summary[19]).toBe('mac · 5 · rename: done')
    expect(actionSummary([{ ...actions[0], queued_at: 400 }], 1000)).toEqual([])
    expect(actionSummary([{ ...actions[0], queued_at: 400.1 }], 1000)).toHaveLength(1)
  })
})
