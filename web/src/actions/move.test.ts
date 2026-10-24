import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session } from './actionFixtures'
import { moveSession, moveSessions } from './move'

it('single local Move opens a prefilled small form, trims destination and explains kept restarts', async () => {
  const row = session('a', { enabled: true })
  const { ctx, state } = actionHarness([row])
  state.answers.push({ cwd: ' /work/other ' })
  await moveSession(ctx, row)
  expect(state.forms[0].title).toBe('Move “Session a”')
  expect(state.forms[0].confirmLabel).toBe('Move')
  expect(state.forms[0].fields[0]).toMatchObject({ key: 'cwd', label: 'Folder', value: '/work/project', suggestions: state.folders })
  expect(state.forms[0].body).toEqual(['Its transcript moves under the new folder, so claudepd --resume works from there.', 'Kept-running sessions restart in the new folder.'])
  expect(state.requests).toEqual([['/api/sessions/a/move', { cwd: '/work/other' }]])
  expect(state.toasts).toEqual([{ message: 'Moved 1 session to /work/other.' }])
})

it('Move cancellation sends nothing', async () => {
  const { ctx, state } = actionHarness([session()])
  await moveSessions(ctx, ['a'])
  expect(state.requests).toEqual([])
  expect(state.toasts).toEqual([])
})

it('bulk Move defaults differing folders to blank, explains blocked/Codex skips and continues in displayed order', async () => {
  const rows = [session('b'), mac(), session('a', { cwd: '/different' }), session('x', { tool: 'codex' }), mac('codex', { tool: 'codex' }),
    session('open', { open_elsewhere: [{ pid: 2, status: 'idle' }] })]
  const { ctx, state } = actionHarness(rows)
  state.answers.push({ cwd: '/target' })
  state.postResults.push(new ApiError(409, 'rename one of them first.'), {}, {})
  await moveSessions(ctx, rows.map((row) => row.id))
  expect(state.forms[0].title).toBe('Move 3 sessions')
  expect(state.forms[0].fields[0]).toMatchObject({ value: '' })
  expect(state.forms[0].body).toContain("2 Codex sessions will be skipped; Codex can't move one (fork it instead).")
  expect(state.forms[0].body).toContain('1 session open, offline or list-only will be skipped.')
  expect(state.requests).toEqual([['/api/sessions/b/move', { cwd: '/target' }], ['/api/hosts/mac/sessions/a/move', { cwd: '/target' }], ['/api/sessions/a/move', { cwd: '/target' }]])
  expect(state.alerts).toEqual([{ title: '1 session failed', body: ['Session b: rename one of them first.'] }])
  expect(state.toasts).toEqual([{ message: 'Local moves done; Mac moves queued. Watch their action status.' }])
})

it.each([
  [mac('a', { host_online: false }), 'Mac is offline; wait for its next push.'],
  [mac('a', { mutable: false }), 'Mac Codex is list-only until its app-server daemon is available.'],
  [mac('a', { state: 'idle' }), 'Open or working on the Mac; close it before changing it.'],
  [session('a', { open_elsewhere: [{ pid: 10, status: 'busy' }] }), 'Open in another claude process (pid 10); close it to rename, move or delete.'],
] as const)('no eligible moves explain the real blocked reason', async (row, message) => {
  const { ctx, state } = actionHarness([row])
  await moveSessions(ctx, [row.id, 'missing'])
  expect(state.toasts).toEqual([{ message }])
  expect(state.forms).toEqual([])
  expect(state.requests).toEqual([])
})

it('Codex-only Move explains Fork instead and sends no request', async () => {
  const row = session('a', { tool: 'codex' })
  const { ctx, state } = actionHarness([row])
  await moveSession(ctx, row)
  expect(state.toasts).toEqual([{ message: "Codex can't move a session; fork it into the folder instead." }])
  expect(state.requests).toEqual([])
})

it('single Mac Move uses a Mac folder form and queue acknowledgement', async () => {
  const row = mac()
  const { ctx, state } = actionHarness([row])
  state.answers.push({ cwd: ' /Users/me/dev/other ' })
  await moveSession(ctx, row)
  expect(state.forms[0].fields[0]).toMatchObject({ label: 'Working folder on the Mac', value: row.cwd, maxLength: 4096 })
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/move', { cwd: '/Users/me/dev/other' }]])
  expect(state.toasts).toEqual([{ message: 'Queued move on mac; watch the action status for its result.' }])
})

it('path refusal is shown as returned without a false success toast', async () => {
  const { ctx, state } = actionHarness([session()])
  state.answers.push({ cwd: 'relative' })
  state.postResults.push(new ApiError(400, 'folder must be an absolute path (or start with ~)'))
  await moveSessions(ctx, ['a'])
  expect(state.requests).toEqual([['/api/sessions/a/move', { cwd: 'relative' }]])
  expect(state.alerts).toEqual([{ title: '1 session failed', body: ['Session a: folder must be an absolute path (or start with ~)'] }])
  expect(state.toasts).toEqual([])
})
