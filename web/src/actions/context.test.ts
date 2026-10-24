import { afterEach, expect, it, vi } from 'vitest'
import { getJson, postJson } from '../api/client'
import { actionHarness, session } from './actionFixtures'
import { renameSession } from './rename'
import { newSession } from './newSession'

afterEach(() => vi.unstubAllGlobals())

it('the page can provide the existing JSON client directly: a form becomes an application/json POST and a server refusal toast', async () => {
  const calls: { path: string; init?: RequestInit }[] = []
  vi.stubGlobal('fetch', async (path: string, init?: RequestInit) => {
    calls.push({ path, init })
    return new Response(JSON.stringify({ error: 'the name must be at most 120 characters' }), { status: 400 })
  })
  const { ctx, state } = actionHarness()
  ctx.post = postJson
  ctx.get = getJson
  state.answers.push({ title: 'New' })
  await renameSession(ctx, session())
  expect(calls.map(({ path, init }) => ({ path, method: init?.method, headers: init?.headers, body: init?.body }))).toEqual([
    { path: '/api/sessions/a/rename', method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"title":"New"}' },
  ])
  expect(state.toasts).toEqual([{ message: 'Rename failed: the name must be at most 120 characters', tone: 'error' }])
  expect(state.delays).toEqual([600])
})

it('GET and POST context signatures also match the creation flow without a transport adapter', async () => {
  const calls: [string, string | undefined, BodyInit | null | undefined][] = []
  vi.stubGlobal('fetch', async (path: string, init?: RequestInit) => {
    calls.push([path, init?.method, init?.body])
    const result = path === '/api/folders' ? { folders: [{ cwd: '/work', project: '/work', sessions: 1, blocked: '', codex_blocked: '' }] }
      : { id: 'made', title: 'New' }
    return new Response(JSON.stringify(result), { status: 200 })
  })
  const { ctx, state } = actionHarness()
  ctx.post = postJson
  ctx.get = getJson
  state.answers.push({ name: 'New' })
  await newSession(ctx)
  expect(calls).toEqual([['/api/folders', undefined, undefined], ['/api/sessions/new', 'POST', '{"tool":"claude","cwd":"/work","name":"New"}']])
  expect(state.toasts).toEqual([{ message: 'Starting “New”…', durationMs: 30000 }])
  expect(ctx.watching.get('made')).toBe('New')
})
