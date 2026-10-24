import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session } from './actionFixtures'
import { renameSession } from './rename'

it('renames local Claude with a prefilled 120-character Title form, trimmed body and success toast', async () => {
  const row = session()
  const { ctx, state } = actionHarness([row])
  state.answers.push({ title: '  New title  ' })
  await renameSession(ctx, row)
  expect(state.forms[0]).toMatchObject({ title: 'Rename “Session a”', confirmLabel: 'Rename', fields: [
    { kind: 'text', key: 'title', label: 'Title', value: 'Session a', required: true, autofocus: true, maxLength: 120 },
  ] })
  expect(state.requests).toEqual([['/api/sessions/a/rename', { title: 'New title' }]])
  expect(state.toasts).toEqual([{ message: 'Renamed to “New title”.' }])
  expect(state.delays).toEqual([600])
})

it('cancel or unchanged title makes no POST', async () => {
  const row = session()
  const { ctx, state } = actionHarness([row])
  await renameSession(ctx, row)
  state.answers.push({ title: ' Session a ' })
  await renameSession(ctx, row)
  expect(state.requests).toEqual([])
  expect(state.toasts).toEqual([])
})

it('local working Codex is allowed to rename; the daemon remains authoritative', async () => {
  const row = session('a', { tool: 'codex', codex_state: 'active' })
  const { ctx, state } = actionHarness([row])
  state.answers.push({ title: 'Codex title' })
  await renameSession(ctx, row)
  expect(state.requests).toEqual([['/api/sessions/a/rename', { title: 'Codex title' }]])
  expect(state.toasts[0].message).toBe('Renamed to “Codex title”.')
})

it.each([400, 409, 503])('preserves a %s refusal and refreshes without a false success', async (code) => {
  const row = session()
  const { ctx, state } = actionHarness([row])
  state.answers.push({ title: 'Duplicate' })
  state.postResults.push(new ApiError(code, "The session is starting, working or waiting on a prompt; rename it when it's idle."))
  await renameSession(ctx, row)
  expect(state.requests).toEqual([['/api/sessions/a/rename', { title: 'Duplicate' }]])
  expect(state.toasts).toEqual([{ message: "Rename failed: The session is starting, working or waiting on a prompt; rename it when it's idle.", tone: 'error' }])
  expect(state.delays).toEqual([600])
})

it('Mac Rename uses a queue toast and never changes the returned row optimistically', async () => {
  const row = mac()
  const { ctx, state } = actionHarness([row])
  state.answers.push({ title: ' Renamed ' })
  await renameSession(ctx, row)
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/rename', { title: 'Renamed' }]])
  expect(state.toasts).toEqual([{ message: 'Queued rename on mac; watch the action status for its result.' }])
  expect(row.title).toBe('Session mac:a')
})

it('Mac Rename retains its queue-on-submit behavior even for an unchanged nonblank title', async () => {
  const row = mac()
  const { ctx, state } = actionHarness([row])
  state.answers.push({ title: ` ${row.title} ` })
  await renameSession(ctx, row)
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/rename', { title: row.title }]])
  expect(state.toasts[0].message).toBe('Queued rename on mac; watch the action status for its result.')
})

it('blocked local and host rename never open the form or POST', async () => {
  const rows = [session('a', { open_elsewhere: [{ pid: 9, status: 'idle' }] }), mac('x', { host_online: false })]
  const { ctx, state } = actionHarness(rows)
  for (const row of rows) await renameSession(ctx, row)
  expect(state.forms).toEqual([])
  expect(state.requests).toEqual([])
  expect(state.toasts.map((toast) => toast.message)).toEqual(['Open in another claude process (pid 9); close it to rename, move or delete.', 'Mac is offline; wait for its next push.'])
})
