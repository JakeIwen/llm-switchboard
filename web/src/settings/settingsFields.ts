// Mirrors settings.py; server-side validation remains authoritative.
const COMMAND = /^[A-Za-z0-9_./-]{1,64}$/
const HIDDEN = /^~(\/[^\s]+)?$/
export interface InstallSettings {
  claude_command: string
  codex_command: string
  folders: { depth: number; min_sessions: number; hidden: string[] }
  poll_seconds: number
}
export interface SettingsValues {
  claude_command: string
  codex_command: string
  'folders.depth': string
  'folders.min_sessions': string
  'folders.hidden': string
  poll_seconds: string
}
interface SettingsField {
  name: keyof SettingsValues
  label: string
  command?: boolean
  range?: readonly [number, number]
  placeholder?: string
}
export const FIELDS: readonly SettingsField[] = [
  { name: 'claude_command', label: 'Claude command', command: true },
  { name: 'codex_command', label: 'Codex command', command: true },
  { name: 'folders.depth', label: 'Folder menu depth', range: [0, 6] },
  { name: 'folders.min_sessions', label: 'Minimum sessions per folder', range: [1, 50] },
  { name: 'folders.hidden', label: 'Hidden folders (comma-separated)', placeholder: '~/private, ~/archive' },
  { name: 'poll_seconds', label: 'Refresh interval (seconds)', range: [2, 60] },
]
const REFRESH_RANGE = FIELDS.find((field) => field.name === 'poll_seconds')!.range!
export const DEFAULT_POLL_SECONDS = 4
export const hiddenFolders = (text: string): string[] => text.split(',').map((part) => part.trim()).filter(Boolean)

export function settingsValues(settings: InstallSettings): SettingsValues {
  return {
    claude_command: settings.claude_command,
    codex_command: settings.codex_command,
    'folders.depth': String(settings.folders.depth),
    'folders.min_sessions': String(settings.folders.min_sessions),
    'folders.hidden': settings.folders.hidden.join(', '),
    poll_seconds: String(settings.poll_seconds),
  }
}
export function settingsUpdate(values: SettingsValues): InstallSettings {
  return {
    claude_command: values.claude_command,
    codex_command: values.codex_command,
    folders: {
      depth: Number(values['folders.depth']),
      min_sessions: Number(values['folders.min_sessions']),
      hidden: hiddenFolders(values['folders.hidden']),
    },
    poll_seconds: Number(values.poll_seconds),
  }
}
export function settingsProblem(values: SettingsValues): string {
  for (const field of FIELDS) {
    const value = values[field.name]
    if (field.command && (!COMMAND.test(value) || /\s/.test(value))) {
      return `${field.name} must be one shell word (1–64 letters, digits, _, ., / or -).`
    }
    if (field.range) {
      const [low, high] = field.range
      if (!/^\d+$/.test(value) || Number(value) < low || Number(value) > high) {
        return `${field.name} must be an integer from ${low} to ${high}.`
      }
    }
  }
  const hidden = hiddenFolders(values['folders.hidden'])
  if (hidden.length > 50 || hidden.some((path) => !HIDDEN.test(path))) {
    return 'folders.hidden must contain at most 50 home-relative paths (~ or ~/folder), without whitespace.'
  }
  return ''
}
export function pollIntervalMs(settings?: { poll_seconds?: unknown } | null): number {
  const seconds = settings?.poll_seconds
  const [low, high] = REFRESH_RANGE
  return (typeof seconds === 'number' && Number.isInteger(seconds) && seconds >= low && seconds <= high ? seconds : DEFAULT_POLL_SECONDS) * 1000
}
