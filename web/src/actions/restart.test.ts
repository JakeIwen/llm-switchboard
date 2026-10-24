import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session, status } from './actionFixtures'
import { restartSession } from './restart'

it('idle kept sessions restart immediately, even in an elsewhere supervisor state', async () => {
  const row = session('a', { enabled: true, status: status({ state: 'elsewhere' }) })
  const { ctx, state } = actionHarness()
  await restartSession(ctx, row)
  expect(state.confirmations).toEqual([])
  expect(state.requests).toEqual([['/api/sessions/a/restart', {}]])
  expect(state.delays).toEqual([600])
})

it('busy restart asks the danger confirmation and supports cancel then confirm', async () => {
  const row = session('a', { enabled: true, status: status({ busy: true }) })
  const { ctx, state } = actionHarness()
  await restartSession(ctx, row)
  expect(state.requests).toEqual([])
  expect(state.confirmations[0]).toEqual({ title: 'Restart it?', body: ["It's working on a turn right now."], confirmLabel: 'Restart', danger: true })
  state.confirms.push(true)
  await restartSession(ctx, row)
  expect(state.requests).toEqual([['/api/sessions/a/restart', {}]])
})

it('shows a returned refusal as is, and refuses inappropriate tools or unkept rows', async () => {
  const { ctx, state } = actionHarness()
  state.postResults.push(new ApiError(409, "a didn't stop within 20s; try again."))
  await restartSession(ctx, session('a', { enabled: true }))
  await restartSession(ctx, session('b'))
  await restartSession(ctx, session('x', { tool: 'codex', enabled: true }))
  await restartSession(ctx, mac())
  expect(state.requests).toEqual([['/api/sessions/a/restart', {}]])
  expect(state.toasts.map((toast) => toast.message)).toEqual(["Restart failed: a didn't stop within 20s; try again.", 'This action is unavailable on the Mac.'])
})
