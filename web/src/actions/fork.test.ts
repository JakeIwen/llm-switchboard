import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session } from './actionFixtures'
import { forkOnMac, forkOntoPi, forkSession } from './fork'

it('local Claude fork proposes a free name, copies while source is open, and watches a kept copy', async () => {
  const row = session('a', { title: 'Work', enabled: true, open_elsewhere: [{ pid: 2, status: 'busy' }] })
  const { ctx, state } = actionHarness([row, session('b', { title: 'Work (fork)' }), session('c', { title: 'Work (fork 2)' })])
  state.answers.push({ cwd: ' /target ' })
  state.postResults.push({ id: 'copy', title: 'Work (fork 3)' })
  await forkSession(ctx, row)
  const form = state.forms[0]
  expect(form.title).toBe('Fork “Work”')
  expect(form.body).toEqual(["Copies the conversation so far into a new Claude Code session. The original is left as it is, even if it's running."])
  expect(form.fields).toMatchObject([
    { key: 'title', label: 'Name', value: 'Work (fork 3)', maxLength: 120, required: true, autofocus: true },
    { key: 'cwd', label: 'Folder', value: row.cwd, suggestions: state.folders },
    { key: 'keep', label: 'Keep it running with Remote Control', value: true },
  ])
  expect(state.requests).toEqual([['/api/sessions/a/fork', { title: 'Work (fork 3)', cwd: '/target', keep: true }]])
  expect([...ctx.watching]).toEqual([['copy', 'Work (fork 3)']])
  expect(state.events).toEqual(['post /api/sessions/a/fork', 'refresh', 'focus copy'])
  expect(state.toasts).toEqual([{ message: 'Forked as “Work (fork 3)”; starting it…' }])
  expect(row.enabled).toBe(true)
})

it('unkept Claude fork defaults keep off but permits keeping only the copy', async () => {
  const row = session('a', { title: 'Work' })
  const { ctx, state } = actionHarness([row])
  state.answers.push({ keep: true })
  state.postResults.push({ id: 'copy', title: 'Work (fork)' })
  await forkSession(ctx, row)
  expect(state.forms[0].fields.at(-1)).toMatchObject({ key: 'keep', value: false })
  expect(state.requests[0][1]).toEqual({ title: 'Work (fork)', cwd: row.cwd, keep: true })
  expect(row.enabled).toBe(false)
  expect(ctx.watching.get('copy')).toBe('Work (fork)')
})

it('Codex fork sends no keep or prompt, starts no watcher and accepts an alternate destination', async () => {
  const row = session('a', { tool: 'codex', title: 'Work', codex_state: 'active' })
  const { ctx, state } = actionHarness([row])
  state.answers.push({})
  state.postResults.push({ id: 'copy', title: 'Work (fork)' })
  await forkSession(ctx, row, '/other')
  expect(state.forms[0].fields.map((field) => field.key)).toEqual(['title', 'cwd'])
  expect(state.requests).toEqual([['/api/sessions/a/fork', { title: 'Work (fork)', cwd: '/other' }]])
  expect(ctx.watching.size).toBe(0)
  expect(state.toasts).toEqual([{ message: 'Forked as “Work (fork)”.' }])
})

it('client duplicate explanation covers both tools and cancel makes no request', async () => {
  const row = session('a', { title: 'Work' })
  const { ctx, state } = actionHarness([row, session('b', { tool: 'codex', title: 'Taken' })])
  state.answers.push((form) => {
    expect(form.validate?.({ title: ' taken ', cwd: row.cwd })).toBe('~/project already has a session named “Taken”.')
    return null
  })
  await forkSession(ctx, row)
  expect(state.requests).toEqual([])
  expect(state.focused).toEqual([])
  expect(ctx.watching.size).toBe(0)
})

it.each([400, 409, 503])('local fork preserves a %s server error and never focuses a nonexistent copy', async (code) => {
  const row = session()
  const { ctx, state } = actionHarness([row])
  state.answers.push({})
  state.postResults.push(new ApiError(code, 'Codex refused: exact reason'))
  await forkSession(ctx, row)
  expect(state.requests).toEqual([['/api/sessions/a/fork', { title: 'Session a (fork)', cwd: row.cwd, keep: false }]])
  expect(state.toasts).toEqual([{ message: 'Fork failed: Codex refused: exact reason', tone: 'error' }])
  expect(state.focused).toEqual([])
})

it.each(['claude', 'codex'] as const)('Mac %s fork queues only title/cwd, with no process or export', async (tool) => {
  const row = mac('a', { tool, title: 'Work' })
  const { ctx, state } = actionHarness([row])
  state.answers.push({ title: 'Copy', cwd: '/Users/me/dev/other' })
  await forkSession(ctx, row)
  expect(state.forms[0]).toMatchObject({ title: 'Fork on the Mac', confirmLabel: 'Queue fork',
    body: ['Copies the conversation on the Mac. No process starts, and the copy is not kept running.'],
    fields: [{ label: 'Name for the copy', value: 'Work (fork)', maxLength: 120 }, { label: 'Existing folder on the Mac', value: row.cwd }] })
  expect(state.forms[0].fields).toHaveLength(2)
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/fork', { title: 'Copy', cwd: '/Users/me/dev/other' }]])
  expect(state.toasts).toEqual([{ message: 'Queued fork on mac; watch the action status for its result.' }])
  expect(ctx.watching.size).toBe(0)
})

it('fork onto Pi has the full explicit-export disclosure and queues only the chosen title', async () => {
  const row = mac()
  const { ctx, state } = actionHarness([row])
  state.answers.push({ title: 'Pi copy' })
  await forkOntoPi(ctx, row)
  expect(state.forms[0]).toMatchObject({ title: 'Fork onto the Pi', confirmLabel: 'Queue fork',
    body: ['Uploads this one transcript to the Pi. ~/<folder> on the Mac maps to ~/<folder> on the Pi, which must exist. The Mac posts a notification and logs the upload (10 per hour; 16 MiB maximum). The copy does not start running.'] })
  expect(state.forms[0].fields).toHaveLength(1)
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/fork-pi', { title: 'Pi copy' }]])
  expect(state.toasts).toEqual([{ message: 'Queued fork-pi on mac; watch the action status for its result.' }])
})

it('Mac fork cancellation/locks and unsupported Codex export never send a POST', async () => {
  const { ctx, state } = actionHarness()
  await forkOnMac(ctx, mac())
  await forkOntoPi(ctx, mac())
  await forkOnMac(ctx, mac('x', { host_online: false }))
  await forkOntoPi(ctx, mac('x', { state: 'busy' }))
  await forkOntoPi(ctx, mac('c', { tool: 'codex' }))
  expect(state.forms).toHaveLength(2)
  expect(state.requests).toEqual([])
  expect(state.toasts.map((toast) => toast.message)).toEqual(['Mac is offline; wait for its next push.', 'Open or working on the Mac; close it before changing it.'])
})

it('fork-pi queue errors retain their mapped-folder guidance', async () => {
  const { ctx, state } = actionHarness()
  state.answers.push({ title: 'Pi copy' })
  state.postResults.push(new ApiError(400, 'Mapped folder does not exist on the Pi: /home/pi/project'))
  await forkOntoPi(ctx, mac())
  expect(state.requests).toEqual([['/api/hosts/mac/sessions/a/fork-pi', { title: 'Pi copy' }]])
  expect(state.toasts[0].message).toBe("Couldn't queue fork-pi: Mapped folder does not exist on the Pi: /home/pi/project")
  expect(state.refreshes).toBe(1)
})
