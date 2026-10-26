import type { SessionRow, SessionStatus } from '../api/sessionRow'
import type { Appearance } from '../settings/appearance'
import type { ActionContext } from './context'
import type { ActionUi, ConfirmOptions, FieldValues, FormOptions, ToastOptions } from './ui'

export function session(id = 'a', changes: Partial<SessionRow> = {}): SessionRow {
  return { id, tool: 'claude', host: '', short_id: id, title: `Session ${id}`, cwd: '/work/project', project: '~/project',
    mtime: 1, size: 1, remote_url: '', resume_cmd: '', new: false, enabled: false, rc_name: '', open_elsewhere: [], status: null, ...changes }
}
export function status(changes: Partial<SessionStatus> = {}): SessionStatus {
  return { session_id: 'a', state: 'running', detail: '', window_id: null, pid: null, rss_kb: null, rc_active: false,
    busy: false, duplicate: false, started_at: null, next_attempt: 0, failures: 0, launches: 0, last_exit: null, rc_name: '', ...changes }
}
export function mac(id = 'a', changes: Partial<SessionRow> = {}): SessionRow {
  return session(`mac:${id}`, { host: 'mac', session_id: id, host_online: true, mutable: true, state: 'closed', ...changes })
}

type FormAnswer = FieldValues | null | ((options: FormOptions) => FieldValues | null)
export function actionHarness(initialRows: SessionRow[] = []) {
  const state = {
    rows: initialRows, payload: { notifications: false, codex: { enabled: false, error: '' } },
    confirmations: [] as ConfirmOptions[], forms: [] as FormOptions[],
    alerts: [] as { title: string; body: readonly string[] }[], toasts: [] as ToastOptions[],
    progress: [] as (string | null)[], confirms: [] as boolean[], answers: [] as FormAnswer[],
    requests: [] as [string, object][], gets: [] as string[],
    postResults: [] as unknown[], getResults: [] as unknown[],
    events: [] as string[], delays: [] as number[], forgotten: [] as string[][],
    focused: [] as string[], revealed: [] as string[], folders: ['/work/project', '/work/other'],
    refreshes: 0, appearance: 'auto' as Appearance,
  }
  const ui: ActionUi = {
    confirm: async (options) => { state.confirmations.push(options); return state.confirms.shift() ?? false },
    form: async (options) => {
      state.forms.push(options)
      const scripted = state.answers.shift() ?? null
      const answer = typeof scripted === 'function' ? scripted(options) : scripted
      if (!answer) return null
      const values: FieldValues = Object.fromEntries(options.fields.map((field) => [field.key,
        field.value ?? (field.kind === 'checkbox' ? false : field.kind === 'select' ? field.options[0]?.value || '' : '')]))
      Object.assign(values, answer)
      const missing = options.fields.find((field) => 'required' in field && field.required && (!field.showWhen || field.showWhen(values)) && !String(values[field.key]).trim())
      const problem = options.validate?.(values)
      if (missing || problem) throw new Error(`Scripted invalid form: ${problem || missing?.key}`)
      return values
    },
    alert: async (options) => { state.alerts.push(options) },
    toast: (options) => { state.toasts.push(options) },
    progress: (text) => { state.progress.push(text) },
  }
  const ctx: ActionContext = {
    ui,
    post: async <T>(path: string, body: object = {}) => {
      state.requests.push([path, body]); state.events.push(`post ${path}`)
      const result = state.postResults.shift()
      if (result instanceof Error) throw result
      return (result ?? {}) as T
    },
    get: async <T>(path: string) => {
      state.gets.push(path)
      const result = state.getResults.shift()
      if (result instanceof Error) throw result
      return result as T
    },
    refresh: async () => { state.refreshes++; state.events.push('refresh') },
    refreshAfter: (ms) => { state.delays.push(ms) }, rows: () => state.rows, payload: () => state.payload,
    knownFolders: () => state.folders, forget: (ids) => { state.forgotten.push([...ids]) },
    focus: (id) => { state.focused.push(id); state.events.push(`focus ${id}`) },
    reveal: (id) => { state.revealed.push(id) }, watching: new Map(),
    appearance: { get: () => state.appearance, set: (value) => { state.appearance = value } },
  }
  return { ctx, state }
}
