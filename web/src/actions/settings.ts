import { APPEARANCE_LABELS, isAppearance, type Appearance } from '../settings/appearance'
import { FIELDS, settingsProblem, settingsUpdate, settingsValues, type InstallSettings, type SettingsValues } from '../settings/settingsFields'
import type { ActionContext } from './context'
import type { FieldValues } from './ui'

function enteredSettings(values: FieldValues): SettingsValues {
  return Object.fromEntries(FIELDS.map((field) => [field.name, String(values[field.name])])) as unknown as SettingsValues
}

export async function openSettings(ctx: ActionContext): Promise<void> {
  let values: SettingsValues
  try { values = settingsValues((await ctx.get<{ settings: InstallSettings }>('/api/settings')).settings) }
  catch (error) { ctx.ui.toast({ message: `Couldn't load settings: ${(error as Error).message}`, tone: 'error' }); return }
  let serverError = ''
  let appearance = ctx.appearance.get()
  while (true) {
    const entered = await ctx.ui.form({
      title: 'Settings', confirmLabel: 'Save',
      body: ['Appearance is for this device. The rest apply to this install, in all browsers.'],
      error: serverError,
      fields: [
        { kind: 'select', key: 'appearance', label: 'Appearance', value: appearance,
          options: (Object.keys(APPEARANCE_LABELS) as Appearance[]).map((value) => ({ value, label: APPEARANCE_LABELS[value] })) },
        ...FIELDS.map((field) => ({ kind: 'text' as const, key: field.name, label: field.label,
          value: values[field.name], placeholder: field.placeholder, mono: field.command })),
      ],
      validate: (raw) => settingsProblem(enteredSettings(raw)),
    })
    if (!entered) return
    if (isAppearance(entered.appearance)) {
      appearance = entered.appearance
      ctx.appearance.set(appearance)
    }
    values = enteredSettings(entered)
    try { await ctx.post('/api/settings', settingsUpdate(values)) }
    catch (error) {
      serverError = `Couldn't save settings: ${(error as Error).message}`
      ctx.ui.toast({ message: serverError, tone: 'error' })
      continue
    }
    await ctx.refresh()
    ctx.ui.toast({ message: 'Settings saved for this install.' })
    return
  }
}
