import { expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { FIELDS, settingsValues, type InstallSettings } from '../settings/settingsFields'
import { actionHarness } from './actionFixtures'
import { openSettings } from './settings'

const settings: InstallSettings = { claude_command: 'claude', codex_command: 'codex', folders: { depth: 2, min_sessions: 2, hidden: [] }, poll_seconds: 4 }

it('loads fresh settings and saves all FIELDS with numeric leaves, depth zero and an empty hidden list', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push({ settings })
  state.answers.push({ claude_command: 'claudep', 'folders.depth': '0', 'folders.hidden': '', poll_seconds: '7' })
  await openSettings(ctx)
  expect(state.gets).toEqual(['/api/settings'])
  expect(state.forms[0]).toMatchObject({ title: 'Settings', confirmLabel: 'Save', body: ['Appearance is for this device. The rest apply to this install, in all browsers.'] })
  expect(state.forms[0].fields.map(({ key, label }) => ({ key, label }))).toEqual([
    { key: 'appearance', label: 'Appearance' }, ...FIELDS.map(({ name, label }) => ({ key: name, label }))])
  expect(state.requests).toEqual([['/api/settings', { ...settings, claude_command: 'claudep', folders: { ...settings.folders, depth: 0 }, poll_seconds: 7 }]])
  expect(state.toasts).toEqual([{ message: 'Settings saved for this install.' }])
  expect(state.refreshes).toBe(1)
})

it('validates commands, numeric ranges and hidden paths through the shared settings validator', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push({ settings })
  state.answers.push((form) => {
    const valid = { ...settingsValues(settings) }
    expect(form.validate?.({ ...valid, claude_command: 'claude --resume' })).toBe('claude_command must be one shell word (1–64 letters, digits, _, ., / or -).')
    expect(form.validate?.({ ...valid, 'folders.depth': '7' })).toBe('folders.depth must be an integer from 0 to 6.')
    expect(form.validate?.({ ...valid, 'folders.hidden': '/absolute' })).toBe('folders.hidden must contain at most 50 home-relative paths (~ or ~/folder), without whitespace.')
    expect(form.validate?.({ ...valid, poll_seconds: '1' })).toBe('poll_seconds must be an integer from 2 to 60.')
    expect(form.validate?.(valid)).toBe('')
    return null
  })
  await openSettings(ctx)
  expect(state.requests).toEqual([])
})

it('server save refusal reopens with raw entered values and the exact error; retry does not reload settings', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push({ settings })
  state.answers.push({ 'folders.hidden': ' ~/private, ,~/archive ', 'folders.depth': '00', poll_seconds: '06' }, {})
  state.postResults.push(new ApiError(400, 'unknown settings key: unexpected'), {})
  await openSettings(ctx)
  expect(state.gets).toEqual(['/api/settings'])
  expect(state.forms[1].error).toBe("Couldn't save settings: unknown settings key: unexpected")
  expect(state.forms[1].fields.find((field) => field.key === 'folders.hidden')?.value).toBe(' ~/private, ,~/archive ')
  expect(state.forms[1].fields.find((field) => field.key === 'folders.depth')?.value).toBe('00')
  expect(state.forms[1].fields.find((field) => field.key === 'poll_seconds')?.value).toBe('06')
  const body = { ...settings, folders: { depth: 0, min_sessions: 2, hidden: ['~/private', '~/archive'] }, poll_seconds: 6 }
  expect(state.requests).toEqual([['/api/settings', body], ['/api/settings', body]])
  expect(state.toasts.map((toast) => toast.message)).toEqual(["Couldn't save settings: unknown settings key: unexpected", 'Settings saved for this install.'])
  expect(state.refreshes).toBe(1)
})

it('cancel on open or after a failed save does not issue further requests', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push({ settings }, { settings })
  await openSettings(ctx)
  expect(state.requests).toEqual([])
  state.answers.push({ claude_command: 'claudep' }, null)
  state.postResults.push(new ApiError(503, 'Settings storage unavailable'))
  await openSettings(ctx)
  expect(state.requests).toEqual([['/api/settings', { ...settings, claude_command: 'claudep' }]])
  expect(state.forms.at(-1)?.error).toBe("Couldn't save settings: Settings storage unavailable")
  expect(state.refreshes).toBe(0)
})

it('load failure is shown as returned and opens no stale form', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push(new ApiError(503, 'Cannot read settings'))
  await openSettings(ctx)
  expect(state.toasts).toEqual([{ message: "Couldn't load settings: Cannot read settings", tone: 'error' }])
  expect(state.forms).toEqual([])
  expect(state.requests).toEqual([])
})

it('appearance defaults to the system and applies to this device on save', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push({ settings })
  state.answers.push({ appearance: 'dark' })
  await openSettings(ctx)
  expect(state.forms[0].fields[0]).toMatchObject({ kind: 'select', value: 'auto',
    options: [{ value: 'auto', label: 'System' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }] })
  expect(state.appearance).toBe('dark')
})

it('cancelling leaves the appearance alone', async () => {
  const { ctx, state } = actionHarness()
  state.getResults.push({ settings })
  state.answers.push(null)
  await openSettings(ctx)
  expect(state.appearance).toBe('auto')
})
