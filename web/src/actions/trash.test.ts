import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session } from './actionFixtures'
import { emptyPiTrash } from './trash'

it('empty trash counts only Pi entries and explains the permanent, local-only deletion', async () => {
  const { ctx, state } = actionHarness()
  state.confirms.push(true)
  await emptyPiTrash(ctx, [session(), mac(), session('codex', { tool: 'codex' })])
  expect(state.confirmations).toEqual([{ title: 'Permanently delete 2 Pi sessions?',
    body: ["This clears only the Pi trash and can't be undone. Mac trash is managed on the Mac."],
    confirmLabel: 'Delete forever', danger: true }])
  expect(state.requests).toEqual([['/api/trash/empty', {}]])
  expect(state.toasts).toEqual([{ message: 'Pi trash emptied. Mac trash stays on the Mac.' }])
  expect(state.refreshes).toBe(1)
})

it('empty/remote-only trash and cancelling the permanent delete never mutate anything', async () => {
  const { ctx, state } = actionHarness()
  await emptyPiTrash(ctx, [])
  await emptyPiTrash(ctx, [mac()])
  expect(state.confirmations).toEqual([])
  await emptyPiTrash(ctx, [session()])
  expect(state.confirmations[0].title).toBe('Permanently delete 1 Pi session?')
  expect(state.requests).toEqual([])
  expect(state.refreshes).toBe(0)
})

it('empty-trash refusal preserves the returned error and refreshes', async () => {
  const { ctx, state } = actionHarness()
  state.confirms.push(true)
  state.postResults.push(new ApiError(503, 'Codex is unavailable'))
  await emptyPiTrash(ctx, [session()])
  expect(state.requests).toEqual([['/api/trash/empty', {}]])
  expect(state.toasts).toEqual([{ message: "Couldn't empty the trash: Codex is unavailable", tone: 'error' }])
  expect(state.refreshes).toBe(1)
})
