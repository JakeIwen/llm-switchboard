import { expect, it } from 'vitest'
import { actionHarness, session, status } from './actionFixtures'
import { checkClaudeReadiness } from './readiness'

it('watches until running, then shows the ready toast/link exactly once', () => {
  const { ctx, state } = actionHarness([session('a', { enabled: true, status: status({ state: 'starting' }) })])
  ctx.watching.set('a', 'New one')
  checkClaudeReadiness(ctx)
  expect(ctx.watching.size).toBe(1)
  expect(state.toasts).toEqual([])
  state.rows = [session('a', { enabled: true, status: status(), remote_url: 'https://example.test/remote' })]
  checkClaudeReadiness(ctx)
  checkClaudeReadiness(ctx)
  expect(state.toasts).toEqual([{ message: '“New one” is ready with Remote Control.', durationMs: 20000, action: { label: 'Open in Claude', href: 'https://example.test/remote' } }])
  expect(ctx.watching.size).toBe(0)
  expect(state.requests).toEqual([])
})

it('running without a URL has no stale toast action', () => {
  const { ctx, state } = actionHarness([session('a', { enabled: true, status: status() })])
  ctx.watching.set('a', 'New one')
  checkClaudeReadiness(ctx)
  expect(state.toasts).toEqual([{ message: '“New one” is ready with Remote Control.', durationMs: 20000 }])
})

it.each(['attention', 'backoff', 'error', 'missing'] as const)('%s stops watching, preserves detail and exposes the Details action', (problem) => {
  const { ctx, state } = actionHarness([session('a', { enabled: true, status: status({ state: problem, detail: 'Accept the prompt.' }) })])
  ctx.watching.set('a', 'Created')
  checkClaudeReadiness(ctx)
  expect(state.toasts[0]).toMatchObject({ message: '“Created” needs attention: Accept the prompt.', durationMs: 20000, action: { label: 'Details' } })
  state.toasts[0].action!.run!()
  expect(state.revealed).toEqual(['a'])
  expect(ctx.watching.size).toBe(0)
})

it('disabled or vanished rows stop silently, while offline/queued rows remain watched', () => {
  const { ctx, state } = actionHarness([session('off'), session('offline', { enabled: true, status: status({ state: 'offline' }) }), session('queued', { enabled: true })])
  for (const id of ['off', 'vanished', 'offline', 'queued']) ctx.watching.set(id, id)
  checkClaudeReadiness(ctx)
  expect([...ctx.watching.keys()]).toEqual(['offline', 'queued'])
  expect(state.toasts).toEqual([])
})
