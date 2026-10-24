import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session, status } from './actionFixtures'
import { setKept, toggleKept } from './keep'

it('enables a local Claude opened elsewhere without preblocking it', async () => {
  const row = session('a', { open_elsewhere: [{ pid: 2, status: 'idle' }] })
  const { ctx, state } = actionHarness([row])
  expect(await toggleKept(ctx, row, true)).toBe(true)
  expect(state.requests).toEqual([['/api/sessions/a/enable', {}]])
  expect(state.confirmations).toEqual([])
  expect(state.delays).toEqual([600])
})

it('cancelling a busy off switch leaves the old checked value and sends nothing', async () => {
  const row = session('a', { enabled: true, status: status({ busy: true }) })
  const { ctx, state } = actionHarness([row])
  expect(await toggleKept(ctx, row, false)).toBe(false)
  expect(row.enabled).toBe(true)
  expect(state.confirmations).toEqual([{ title: 'Stop keeping it running?', body: ["It's working on a turn right now."], confirmLabel: 'Stop', danger: true }])
  expect(state.requests).toEqual([])
  state.confirms.push(true)
  expect(await toggleKept(ctx, row, false)).toBe(true)
  expect(state.requests).toEqual([['/api/sessions/a/disable', {}]])
})

it('disables idle sessions directly and returns false for unchanged switches', async () => {
  const row = session('a', { enabled: true })
  const { ctx, state } = actionHarness([row])
  expect(await toggleKept(ctx, row, true)).toBe(false)
  expect(await toggleKept(ctx, row, false)).toBe(true)
  expect(state.requests).toEqual([['/api/sessions/a/disable', {}]])
  expect(state.confirmations).toEqual([])
})

it('asks the memory confirmation only for more than two changed local Claude rows and honours cancel', async () => {
  const rows = [session('b'), session('a'), session('c'), session('d', { enabled: true }), mac(), session('x', { tool: 'codex' })]
  const { ctx, state } = actionHarness(rows)
  await setKept(ctx, rows.map((row) => row.id), true)
  expect(state.confirmations[0]).toEqual({ title: 'Keep 3 sessions running?', items: ['Session b', 'Session a', 'Session c'], body: ['Each is a claude process using roughly 300 MB.'], confirmLabel: 'Keep running' })
  expect(state.requests).toEqual([])
  state.confirms.push(true)
  await setKept(ctx, rows.map((row) => row.id), true)
  expect(state.requests).toEqual([['/api/sessions/b/enable', {}], ['/api/sessions/a/enable', {}], ['/api/sessions/c/enable', {}]])
  expect(state.toasts.at(-1)?.message).toBe('Keeping 3 sessions running. Skipped 1 Mac sessions.')
})

it('bulk stop confirms busy changed targets, skips Codex/Mac/unchanged rows and reports partial failure', async () => {
  const rows = [session('a', { enabled: true, status: status({ busy: true }) }), session('b', { enabled: true }), session('c'), mac(), session('x', { tool: 'codex' })]
  const { ctx, state } = actionHarness(rows)
  state.confirms.push(true)
  state.postResults.push(new ApiError(409, 'Cannot stop now'), {})
  await setKept(ctx, rows.map((row) => row.id), false)
  expect(state.confirmations[0]).toEqual({ title: 'Stop 2 sessions?', body: ['1 session working on a turn right now.'], confirmLabel: 'Stop', danger: true })
  expect(state.requests).toEqual([['/api/sessions/a/disable', {}], ['/api/sessions/b/disable', {}]])
  expect(state.alerts).toEqual([{ title: '1 session failed', body: ['Session a: Cannot stop now'] }])
  expect(state.toasts.at(-1)?.message).toBe('Stopped keeping 1 session. Skipped 1 Mac sessions.')
})

it('bulk idle stop needs no confirmation; two enables need none either', async () => {
  const { ctx, state } = actionHarness([session('a', { enabled: true }), session('b')])
  await setKept(ctx, ['a'], false)
  await setKept(ctx, ['b'], true)
  expect(state.requests).toEqual([['/api/sessions/a/disable', {}], ['/api/sessions/b/enable', {}]])
  expect(state.confirmations).toEqual([])
  expect(state.toasts.map((toast) => toast.message)).toEqual(['Stopped keeping 1 session.', 'Keeping 1 session running.'])
})

it.each([
  [[mac()], true, '1 Mac sessions skipped: the Pi cannot keep them running.'],
  [[mac(), session('a', { enabled: true })], true, '1 Mac sessions skipped; no selected Pi Claude session needs this change.'],
  [[session('a', { tool: 'codex' })], true, 'Codex sessions have no keep-running; the Codex daemon holds them all.'],
  [[session('a', { enabled: true })], true, 'Already kept running.'],
  [[session()], false, 'None of these are kept running.'],
] as const)('explains a no-target keep/stop selection', async (rows, on, message) => {
  const { ctx, state } = actionHarness([...rows])
  await setKept(ctx, [...rows.map((row) => row.id), 'missing'], on)
  expect(state.requests).toEqual([])
  expect(state.toasts).toEqual([{ message }])
})

it('cancelling bulk Stop or a refused individual Keep leaves the catalogue untouched', async () => {
  const row = session('a', { enabled: true, status: status({ busy: true }) })
  const { ctx, state } = actionHarness([row])
  await setKept(ctx, ['a'], false)
  expect(state.requests).toEqual([])
  expect(state.confirmations[0].title).toBe('Stop 1 session?')
  state.postResults.push(new ApiError(404, 'no transcript with that session id'))
  expect(await toggleKept(ctx, session('b'), true)).toBe(false)
  expect(state.requests).toEqual([['/api/sessions/b/enable', {}]])
  expect(state.toasts).toEqual([{ message: 'Enable failed: no transcript with that session id', tone: 'error' }])
  expect(row.enabled).toBe(true)
})

it('rejects inappropriate single-row keep actions without any host POST', async () => {
  const { ctx, state } = actionHarness()
  await toggleKept(ctx, mac(), true)
  await toggleKept(ctx, session('x', { tool: 'codex' }), true)
  expect(state.requests).toEqual([])
  expect(state.toasts.map((toast) => toast.message)).toEqual(['This action is unavailable on the Mac.', 'Codex sessions have no keep-running; the Codex daemon holds them all.'])
})
