import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session, status } from './actionFixtures'
import { deleteSession, deleteSessions, restoreSessions } from './deleteRestore'

it('confirms a kept/working local delete, forgets it after success, and Undo restores without reenabling', async () => {
  const row = session('a', { enabled: true, status: status({ busy: true }) })
  const { ctx, state } = actionHarness([row])
  state.confirms.push(true)
  state.postResults.push({ trashed: true })
  await deleteSession(ctx, row)
  expect(state.confirmations[0]).toEqual({ title: 'Delete “Session a”?', body: [
    'It goes to Recently deleted for 30 days.', "It's kept running and will be stopped.", "It's working on a turn right now.",
  ], items: [], confirmLabel: 'Delete', danger: true })
  expect(state.requests).toEqual([['/api/sessions/a/delete', {}]])
  expect(state.forgotten).toEqual([['a']])
  expect(state.toasts[0].message).toBe('Deleted “Session a”.')
  expect(state.toasts[0].action?.label).toBe('Undo')
  state.rows = []
  await state.toasts[0].action!.run!()
  expect(state.requests).toEqual([['/api/sessions/a/delete', {}], ['/api/sessions/a/restore', {}]])
  expect(state.toasts.at(-1)?.message).toBe('Restored 1 session.')
  expect(state.refreshes).toBe(2)
})

it('never offers Undo or 30-day wording for a never-written session', async () => {
  const row = session('a', { new: true, enabled: true })
  const { ctx, state } = actionHarness([row])
  state.confirms.push(true)
  state.postResults.push({ trashed: false })
  await deleteSession(ctx, row)
  expect(state.confirmations[0].body?.join(' ')).not.toContain('30 days')
  expect(state.confirmations[0].body).toContain('Sessions with no transcript are simply stopped and forgotten; there is nothing to restore.')
  expect(state.requests).toEqual([['/api/sessions/a/delete', {}]])
  expect(state.toasts).toEqual([{ message: 'Deleted “Session a”.' }])
})

it('uses actual delete responses for Undo, even if a pending transcript was written after the catalogue fetch', async () => {
  const { ctx, state } = actionHarness([session('a', { new: true }), session('b')])
  state.confirms.push(true)
  state.postResults.push({ trashed: true }, { trashed: false })
  await deleteSessions(ctx, ['a', 'b'])
  expect(state.confirmations[0].body).toContain('1 session will go to Recently deleted for 30 days.')
  await state.toasts[0].action!.run!()
  expect(state.requests).toEqual([['/api/sessions/a/delete', {}], ['/api/sessions/b/delete', {}], ['/api/sessions/a/restore', {}]])
})

it('cancel and all-blocked deletion make no requests', async () => {
  const { ctx, state } = actionHarness([session(), mac('x', { host_online: false })])
  await deleteSessions(ctx, ['a'])
  expect(state.confirmations[0].title).toBe('Delete “Session a”?')
  await deleteSessions(ctx, ['mac:x', 'missing'])
  expect(state.requests).toEqual([])
  expect(state.toasts).toEqual([{ message: 'Selected sessions are open, offline or list-only; close them or wait for the Mac.' }])
})

it('bulk delete partitions locked rows, continues after active Codex refusal and only undoes successful local trash', async () => {
  const rows = [session('a'), session('busy', { tool: 'codex', codex_state: 'active' }), session('b'),
    session('open', { open_elsewhere: [{ pid: 10, status: 'idle' }] }), mac('offline', { host_online: false })]
  const { ctx, state } = actionHarness(rows)
  state.confirms.push(true)
  state.postResults.push({ trashed: true }, new ApiError(409, "It's working on a turn; delete it once it's done."), { trashed: true })
  await deleteSessions(ctx, ['a', 'busy', 'missing', 'open', 'b', 'mac:offline'])
  expect(state.confirmations[0].title).toBe('Delete 3 sessions?')
  expect(state.confirmations[0].body).toContain('2 sessions open, offline or list-only will be skipped.')
  expect(state.confirmations[0].items).toEqual(['Session a', 'Session busy', 'Session b'])
  expect(state.requests).toEqual([['/api/sessions/a/delete', {}], ['/api/sessions/busy/delete', {}], ['/api/sessions/b/delete', {}]])
  expect(state.forgotten).toEqual([['a', 'b']])
  expect(state.alerts).toEqual([{ title: '1 session failed', body: ["Session busy: It's working on a turn; delete it once it's done."] }])
  expect(state.toasts[0].message).toBe('Deleted 2 sessions.')
  await state.toasts[0].action!.run!()
  expect(state.requests.slice(3)).toEqual([['/api/sessions/a/restore', {}], ['/api/sessions/b/restore', {}]])
})

it('mixed deletes route host ids correctly and replace Undo with queued guidance only if a remote delete succeeded', async () => {
  const { ctx, state } = actionHarness([session(), mac()])
  state.confirms.push(true)
  await deleteSessions(ctx, ['mac:a', 'a'])
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/delete', {}], ['/api/sessions/a/delete', {}]])
  expect(state.toasts).toEqual([{ message: 'Local deletes done; Mac deletes queued. Watch their action status.' }])
  expect(state.forgotten).toEqual([['mac:a', 'a']])
})

it('a failed remote delete does not remove local Undo', async () => {
  const { ctx, state } = actionHarness([session(), mac()])
  state.confirms.push(true)
  state.postResults.push(new ApiError(503, 'host queue unavailable'), { trashed: true })
  await deleteSessions(ctx, ['mac:a', 'a'])
  expect(state.alerts[0]).toEqual({ title: '1 session failed', body: ['Session mac:a: host queue unavailable'] })
  expect(state.toasts[0].action?.label).toBe('Undo')
  expect(state.forgotten).toEqual([['a']])
})

it('Mac row delete has the explicit no-upload queue confirmation and honours cancellation', async () => {
  const { ctx, state } = actionHarness()
  await deleteSession(ctx, mac())
  expect(state.confirmations[0]).toEqual({ title: 'Delete “Session mac:a” on the Mac?',
    body: ["Queues a move to the Mac's trash for 30 days. No transcript is uploaded."], confirmLabel: 'Queue delete', danger: true })
  expect(state.requests).toEqual([])
  state.confirms.push(true)
  await deleteSession(ctx, mac())
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/delete', {}]])
  expect(state.toasts).toEqual([{ message: 'Queued delete on mac; watch the action status for its result.' }])
})

it('restore uses trash titles for partial failure, skips locked hosts and never confirms', async () => {
  const { ctx, state } = actionHarness()
  state.postResults.push(new ApiError(400, '/work/project already exists'), {})
  await restoreSessions(ctx, [session('a'), mac(), mac('offline', { host_online: false })])
  expect(state.requests).toEqual([['/api/sessions/a/restore', {}], ['/api/hosts/mac/sessions/a/restore', {}]])
  expect(state.confirmations).toEqual([])
  expect(state.alerts).toEqual([{ title: '1 session failed', body: ['Session a: /work/project already exists'] }])
  expect(state.toasts).toEqual([{ message: 'Mac restores queued; watch their action status.' }])
})

it('all failed delete has no success toast or Undo, and a locked restore sends nothing', async () => {
  const { ctx, state } = actionHarness([session()])
  state.confirms.push(true)
  state.postResults.push(new ApiError(409, "a didn't stop within 20s; try again."))
  await deleteSessions(ctx, ['a'])
  expect(state.toasts).toEqual([])
  expect(state.alerts[0]).toEqual({ title: '1 session failed', body: ["Session a: a didn't stop within 20s; try again."] })
  await restoreSessions(ctx, [mac('x', { mutable: false })])
  expect(state.requests).toEqual([['/api/sessions/a/delete', {}]])
  expect(state.toasts[0].message).toBe('Mac Codex is list-only until its app-server daemon is available.')
})
