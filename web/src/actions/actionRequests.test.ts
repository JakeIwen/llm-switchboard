import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac } from './actionFixtures'
import { postLocalAction, queueHostAction } from './actionRequests'

it('sends an empty JSON object and schedules the legacy single-action refresh after success', async () => {
  const { ctx, state } = actionHarness()
  expect(await postLocalAction(ctx, 'a', 'restart')).toBe(true)
  expect(state.requests).toEqual([['/api/sessions/a/restart', {}]])
  expect(state.delays).toEqual([600])
  expect(state.refreshes).toBe(0)
  expect(state.toasts).toEqual([])
})

it.each([400, 409, 503])('keeps a %s server refusal verbatim inside the generic failure toast and still schedules refresh', async (code) => {
  const { ctx, state } = actionHarness()
  state.postResults.push(new ApiError(code, 'Exact server reason “unchanged”.'))
  expect(await postLocalAction(ctx, 'a', 'rename', { title: 'New title' })).toBe(false)
  expect(state.requests).toEqual([['/api/sessions/a/rename', { title: 'New title' }]])
  expect(state.toasts).toEqual([{ message: 'Rename failed: Exact server reason “unchanged”.', tone: 'error' }])
  expect(state.delays).toEqual([600])
})

it('queues only through the Pi host route and never optimistically changes metadata', async () => {
  const row = mac('a')
  const { ctx, state } = actionHarness([row])
  expect(await queueHostAction(ctx, row, 'rename', { title: 'Copy' })).toBe(true)
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/rename', { title: 'Copy' }]])
  expect(row.title).toBe('Session mac:a')
  expect(state.toasts).toEqual([{ message: 'Queued rename on mac; watch the action status for its result.' }])
  expect(state.refreshes).toBe(1)
})

it('shows a queue conflict unchanged and refreshes after refusal', async () => {
  const { ctx, state } = actionHarness()
  state.postResults.push(new ApiError(409, 'This Mac session already has an action waiting for a result.'))
  await queueHostAction(ctx, mac(), 'delete', {})
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/delete', {}]])
  expect(state.toasts[0].message).toBe("Couldn't queue delete: This Mac session already has an action waiting for a result.")
  expect(state.refreshes).toBe(1)
})

it.each([
  [mac('a', { host_online: false }), 'Mac is offline; wait for its next push.'],
  [mac('a', { mutable: false }), 'Mac Codex is list-only until its app-server daemon is available.'],
  [mac('a', { state: 'busy' }), 'Open or working on the Mac; close it before changing it.'],
] as const)('prevents a locked host mutation', async (row, reason) => {
  const { ctx, state } = actionHarness()
  expect(await queueHostAction(ctx, row, 'delete', {})).toBe(false)
  expect(state.requests).toEqual([])
  expect(state.toasts).toEqual([{ message: reason }])
  expect(state.refreshes).toBe(0)
})
