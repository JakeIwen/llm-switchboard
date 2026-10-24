import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, session } from './actionFixtures'
import { newSession, type NewSessionFolder } from './newSession'

const folder: NewSessionFolder = { cwd: '/work/project', project: '~/project', sessions: 2, blocked: '', codex_blocked: '' }
const untrusted: NewSessionFolder = { cwd: '/work/untrusted', project: '~/untrusted', sessions: 1, blocked: 'Not trusted yet; open Claude Code there once in a terminal to accept the trust prompt.', codex_blocked: '' }
const missing: NewSessionFolder = { cwd: '/gone', project: '~/gone', sessions: 1, blocked: 'The folder no longer exists.', codex_blocked: 'The folder no longer exists.' }

it('new Claude loads allowed folders, honours a preferred local folder, and starts the readiness watch', async () => {
  const { ctx, state } = actionHarness()
  state.payload.notifications = true
  state.getResults.push({ folders: [folder, { ...folder, cwd: '/other', project: '~/other', sessions: 1 }, missing] })
  state.answers.push({ name: 'New' })
  state.postResults.push({ id: 'made', title: 'New' })
  await newSession(ctx, { preferredFolder: '~/other' })
  expect(state.gets).toEqual(['/api/folders'])
  expect(state.forms[0]).toMatchObject({ title: 'New session', confirmLabel: 'Create', fields: [
    { key: 'cwd', label: 'Folder', value: '/other', options: [{ value: folder.cwd, label: '~/project  (2 sessions)' }, { value: '/other', label: '~/other  (1 session)' }] },
    { key: 'name', label: 'Name', required: true, autofocus: true, maxLength: 120, placeholder: 'What is this session for?' },
  ] })
  expect(state.forms[0].body).toContain("starts through claudepd with Remote Control on, and is kept running. You'll get a notification when it's ready.")
  expect(state.forms[0].body).toContain('~/gone: The folder no longer exists.')
  expect(state.requests).toEqual([['/api/sessions/new', { tool: 'claude', cwd: '/other', name: 'New' }]])
  expect([...ctx.watching]).toEqual([['made', 'New']])
  expect(state.toasts).toEqual([{ message: 'Starting “New”…', durationMs: 30000 }])
  expect(state.events).toEqual(['post /api/sessions/new', 'refresh', 'focus made'])
})

it('healthy Codex offers tool-specific folders and a conditional required first message; creating starts work, not a Claude watch', async () => {
  const { ctx, state } = actionHarness()
  state.payload.codex.enabled = true
  state.getResults.push({ folders: [folder, untrusted, missing] })
  state.answers.push({ cwd: untrusted.cwd, name: 'New Codex', prompt: 'Do the work' })
  state.postResults.push({ id: 'made', title: 'New Codex' })
  await newSession(ctx, { tool: 'codex' })
  const form = state.forms[0]
  expect(form.fields[0]).toMatchObject({ key: 'tool', value: 'codex', options: [{ value: 'claude', label: 'Claude Code' }, { value: 'codex', label: 'Codex' }] })
  expect(form.fields[1]).toMatchObject({ key: 'cwd', options: [{ value: folder.cwd }, { value: untrusted.cwd }] })
  const prompt = form.fields.at(-1)!
  expect(prompt).toMatchObject({ key: 'prompt', label: 'First message', required: true, maxLength: 20000, placeholder: 'What should Codex do first?' })
  expect(prompt.showWhen?.({ tool: 'claude' })).toBe(false)
  expect(prompt.showWhen?.({ tool: 'codex' })).toBe(true)
  expect(form.body).toContain('Codex: starts in the Codex daemon and works on your first message right away, without asking for approvals (as codexp). Codex saves a session only once it has a message.')
  expect(form.validate?.({ tool: 'claude', cwd: untrusted.cwd, name: 'New' })).toBe(`Claude Code can't start here: ${untrusted.blocked}`)
  expect(form.validate?.({ tool: 'codex', cwd: missing.cwd, name: 'New', prompt: 'go' })).toBe("Codex can't start here: The folder no longer exists.")
  expect(state.requests).toEqual([['/api/sessions/new', { tool: 'codex', cwd: untrusted.cwd, name: 'New Codex', prompt: 'Do the work' }]])
  expect(state.toasts).toEqual([{ message: "Started “New Codex” in Codex; it's working on your first message.", durationMs: 15000 }])
  expect(ctx.watching.size).toBe(0)
})

it('Codex first-message validation measures the complete serialized UTF-8 body, including JSON escaping', async () => {
  const { ctx, state } = actionHarness()
  state.payload.codex.enabled = true
  state.getResults.push({ folders: [folder] })
  state.answers.push((form) => {
    const values = { tool: 'codex', cwd: folder.cwd, name: 'New', prompt: '' }
    const budget = 16 * 1024 - new TextEncoder().encode(JSON.stringify(values)).byteLength
    expect(form.validate?.({ ...values, prompt: 'a'.repeat(budget) })).toBe('')
    expect(form.validate?.({ ...values, prompt: 'a'.repeat(budget + 1) })).toBe('request body too large')
    expect(form.validate?.({ ...values, prompt: '界'.repeat(6000) })).toBe('request body too large')
    expect(form.validate?.({ ...values, prompt: '\n'.repeat(8500) })).toBe('request body too large')
    expect(form.validate?.({ ...values, name: '界'.repeat(120), prompt: 'a'.repeat(budget - 100) })).toBe('request body too large')
    return null
  })
  await newSession(ctx, { tool: 'codex' })
  expect(state.requests).toEqual([])
})

it.each([
  { enabled: true, error: 'daemon unavailable' },
  { enabled: false, error: '' },
])('unhealthy/off Codex stays Claude-only, even when a Codex tool filter was selected', async (codex) => {
  const { ctx, state } = actionHarness([session('stale', { tool: 'codex' })])
  state.payload.codex = codex
  state.getResults.push({ folders: [folder, untrusted] })
  state.answers.push({ name: 'Claude only' })
  state.postResults.push({ id: 'made', title: 'Claude only' })
  await newSession(ctx, { tool: 'codex' })
  expect(state.forms[0].fields.map((field) => field.key)).toEqual(['cwd', 'name'])
  expect(state.requests).toEqual([['/api/sessions/new', { tool: 'claude', cwd: folder.cwd, name: 'Claude only' }]])
  expect(ctx.watching.get('made')).toBe('Claude only')
})

it('Claude ignores the conditional required prompt while keeping its configured hidden field in the JSON body', async () => {
  const { ctx, state } = actionHarness()
  state.payload.codex.enabled = true
  state.getResults.push({ folders: [folder] })
  state.answers.push({ name: 'Claude' })
  state.postResults.push({ id: 'made', title: 'Claude' })
  await newSession(ctx)
  expect(state.requests).toEqual([['/api/sessions/new', { tool: 'claude', cwd: folder.cwd, name: 'Claude', prompt: '' }]])
})

it('no usable folders produce an OK-only explanation instead of a creation form', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push({ folders: [untrusted, missing] })
  await newSession(ctx)
  expect(state.alerts[0].title).toBe('No folder is available')
  expect(state.alerts[0].body).toContain(`~/untrusted: ${untrusted.blocked}`)
  expect(state.forms).toEqual([])
  expect(state.requests).toEqual([])
})

it('folder-load refusal is shown unchanged and cancel never sends a POST', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push(new ApiError(503, 'Service warming up'), { folders: [folder] })
  await newSession(ctx)
  await newSession(ctx)
  expect(state.toasts).toEqual([{ message: "Couldn't load folders: Service warming up", tone: 'error' }])
  expect(state.requests).toEqual([])
  expect(state.forms).toHaveLength(1)
})

it('new-session client duplicate detection includes local Codex but not remote names', async () => {
  const { ctx, state } = actionHarness([session('x', { tool: 'codex', title: 'Taken' })])
  state.getResults.push({ folders: [folder] })
  state.answers.push((form) => {
    expect(form.validate?.({ cwd: folder.cwd, name: ' TAKEN ' })).toBe('~/project already has a session named “Taken”.')
    return null
  })
  await newSession(ctx)
  expect(state.requests).toEqual([])
})

it.each([400, 409, 503])('new-session %s refusal never starts a readiness watch or announces success', async (code) => {
  const { ctx, state } = actionHarness()
  state.getResults.push({ folders: [folder] })
  state.answers.push({ name: 'New' })
  state.postResults.push(new ApiError(code, 'Exact creation refusal.'))
  await newSession(ctx)
  expect(state.requests).toEqual([['/api/sessions/new', { tool: 'claude', cwd: folder.cwd, name: 'New' }]])
  expect(state.toasts).toEqual([{ message: "Couldn't create it: Exact creation refusal.", tone: 'error' }])
  expect(ctx.watching.size).toBe(0)
  expect(state.focused).toEqual([])
})
