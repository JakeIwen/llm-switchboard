import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { actionHarness, mac, session, status } from './actionFixtures'
import { remoteControlName } from './remoteControlName'

it('shows a 120-character RC name form, even for open-elsewhere sessions, without restarting', async () => {
  const row = session('a', { rc_name: 'Old', status: status({ rc_name: 'Launched name' }), open_elsewhere: [{ pid: 9, status: 'busy' }] })
  const { ctx, state } = actionHarness([row])
  state.answers.push({ name: '  New name  ' })
  await remoteControlName(ctx, row)
  expect(state.forms[0]).toMatchObject({ title: 'Remote Control name for “Session a”', confirmLabel: 'Save', fields: [
    { key: 'name', label: 'Remote Control name (blank = session title; applies on next launch)', value: 'Old', placeholder: 'Launched name', maxLength: 120 },
  ] })
  expect(state.requests).toEqual([['/api/sessions/a/name', { name: '  New name  ' }]])
  expect(state.toasts).toEqual([])
  expect(state.delays).toEqual([600])
})

it('blank resets the fallback while cancellation sends nothing', async () => {
  const row = session()
  const { ctx, state } = actionHarness([row])
  await remoteControlName(ctx, row)
  state.answers.push({ name: '' })
  await remoteControlName(ctx, row)
  expect(state.forms[0].fields[0]).toMatchObject({ placeholder: row.title })
  expect(state.requests).toEqual([['/api/sessions/a/name', { name: '' }]])
})

it('retains the server name refusal and never offers RC name for Mac or Codex', async () => {
  const { ctx, state } = actionHarness()
  state.answers.push({ name: 'Invalid' })
  state.postResults.push(new ApiError(400, 'name must be a string of at most 120 characters'))
  await remoteControlName(ctx, session())
  await remoteControlName(ctx, session('x', { tool: 'codex' }))
  await remoteControlName(ctx, mac())
  expect(state.forms).toHaveLength(1)
  expect(state.requests).toEqual([['/api/sessions/a/name', { name: 'Invalid' }]])
  expect(state.toasts.map((toast) => toast.message)).toEqual(['Name failed: name must be a string of at most 120 characters', 'This action is unavailable on the Mac.'])
})
