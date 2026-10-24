import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session } from './actionFixtures'
import { runBulk, summaryItems } from './actionBatch'

it('runs serially in supplied order, keeps successes, clears progress and reports every refusal after refresh', async () => {
  const rows = [session('b'), session('a'), session('c')]
  const { ctx, state } = actionHarness(rows)
  const events: string[] = []
  let active = 0
  const done = await runBulk(ctx, 'Deleting', rows, async (id) => {
    expect(active++).toBe(0)
    events.push(`start ${id}`)
    await Promise.resolve()
    active--
    events.push(`finish ${id}`)
    if (id !== 'a') throw new ApiError(id === 'b' ? 409 : 503, id === 'b' ? 'still working' : 'daemon unavailable')
  })
  expect(events).toEqual(['start b', 'finish b', 'start a', 'finish a', 'start c', 'finish c'])
  expect(done).toEqual(['a'])
  expect(state.progress).toEqual(['Deleting 1 of 3…', 'Deleting 2 of 3…', 'Deleting 3 of 3…', null])
  expect(state.refreshes).toBe(1)
  expect(state.alerts).toEqual([{ title: '2 sessions failed', body: ['Session b: still working\nSession c: daemon unavailable'] }])
})

it('uses the single-target progress label and stable trash titles absent from live rows', async () => {
  const { ctx, state } = actionHarness()
  await runBulk(ctx, 'Restoring', [mac('a')], (id) => ctx.post(`/restore/${id}`, {}))
  expect(state.requests).toEqual([['/restore/mac:a', {}]])
  expect(state.progress).toEqual(['Restoring…', null])
  expect(state.alerts).toEqual([])
})

it('limits confirmation title lists to eight with a bounded remainder', () => {
  const rows = Array.from({ length: 11 }, (_, n) => session(String(n)))
  expect(summaryItems(rows)).toEqual([...rows.slice(0, 8).map((row) => row.title), '…and 3 more'])
  expect(summaryItems(rows.slice(0, 1))).toEqual([])
})
