import { expect, it } from 'vitest'
import { BulkActions, splitBlocked } from './bulk'
import { sessionBlockedReason } from './badges'

it('runs bulk actions sequentially and exposes progress', async () => {
  const sessions = new Map([['a', { title: 'Alpha' }], ['b', { title: 'Beta' }]])
  const progress: string[] = [], calls: string[] = []
  const runner = new BulkActions({
    lookup: (id) => sessions.get(id), showProgress: (value) => progress.push(value),
    refresh: async () => calls.push('refresh'), reportFailures: async () => { throw Error('unexpected failure') },
  })
  const done = await runner.run('Moving', ['a', 'b'], async (id) => { calls.push(id) })
  expect(done).toEqual(['a', 'b'])
  expect(calls).toEqual(['a', 'b', 'refresh'])
  expect(progress).toEqual(['Moving 1 of 2…', 'Moving 2 of 2…', ''])
})

it('partitions blocked rows, ignoring ids no longer in the catalogue', () => {
  const rows = new Map([
    ['a', { id: 'a', title: 'Alpha', host: '', open_elsewhere: [] }],
    ['b', { id: 'b', title: 'Beta', host: '', open_elsewhere: [{ pid: 42, status: 'idle' }] }],
  ])
  const partition = splitBlocked(['a', 'b', 'gone'], (id: string) => rows.get(id), sessionBlockedReason)
  expect(partition.targets.map((row) => row.id)).toEqual(['a'])
  expect(partition.blocked.map((row) => row.id)).toEqual(['b'])
  expect(partition.targets[0]).toBe(rows.get('a'))
  expect(splitBlocked([], (id: string) => rows.get(id), sessionBlockedReason)).toEqual({ targets: [], blocked: [] })
})

it('awaits each action, continues failures, refreshes then reports named errors', async () => {
  const calls: string[] = []
  let finish!: () => void
  const first = new Promise<void>((resolve) => { finish = resolve })
  const runner = new BulkActions({
    lookup: (id) => id === 'b' ? { title: 'Beta' } : undefined,
    showProgress: (value) => { calls.push(value) }, refresh: async () => { calls.push('refresh') },
    reportFailures: async (failure) => {
      expect(calls.at(-1)).toBe('refresh')
      expect(failure).toEqual({ title: '2 sessions failed', body: 'Beta: refused\nc: missing', confirm: 'OK' })
      calls.push('reported')
    },
  })
  const running = runner.run('Moving', ['a', 'b', 'c', 'd'], async (id) => {
    calls.push(id)
    if (id === 'a') await first
    if (id === 'b') throw Error('refused')
    if (id === 'c') throw Error('missing')
  })
  expect(calls).toEqual(['Moving 1 of 4…', 'a'])
  finish()
  expect(await running).toEqual(['a', 'd'])
  expect(calls).toEqual(['Moving 1 of 4…', 'a', 'Moving 2 of 4…', 'b', 'Moving 3 of 4…', 'c', 'Moving 4 of 4…', 'd', '', 'refresh', 'reported'])
})

it('uses singular progress and failure titles, and still refreshes for empty input', async () => {
  const calls: unknown[] = []
  const runner = new BulkActions({
    lookup: () => ({ title: '' }), showProgress: (value) => { calls.push(value) },
    refresh: async () => { calls.push('refresh') }, reportFailures: async (failure) => { calls.push(failure) },
  })
  expect(await runner.run('Deleting', ['a'], async () => { throw Error('no') })).toEqual([])
  expect(calls).toEqual(['Deleting…', '', 'refresh', { title: '1 session failed', body: 'a: no', confirm: 'OK' }])
  calls.length = 0
  expect(await runner.run('Deleting', [], async () => { throw Error('unreachable') })).toEqual([])
  expect(calls).toEqual(['', 'refresh'])
})

it('does not report failures if refresh itself rejects', async () => {
  let reported = false
  const runner = new BulkActions({ lookup: () => null, showProgress: () => {},
    refresh: async () => { throw Error('offline') }, reportFailures: async () => { reported = true } })
  await expect(runner.run('Deleting', ['a'], async () => { throw Error('refused') })).rejects.toThrow('offline')
  expect(reported).toBe(false)
})
