import { TOOLS, type SessionTool } from '../api/sessionRow'
import { plural } from '../format/format'
import { MAX_NAME_LENGTH, nameTaken } from './actionNames'
import type { ActionContext, CreatedSession } from './context'
import type { FieldValues, FormField, FormOptions } from './ui'

/** GET /api/folders; blocked reasons are returned by the server, not inferred here. */
export interface NewSessionFolder {
  cwd: string
  project: string
  sessions: number
  blocked: string
  codex_blocked: string
}
export interface NewSessionOptions { tool?: SessionTool; preferredFolder?: string }

const MAX_BODY_BYTES = 16 * 1024
const TOOL_NAME = { claude: 'Claude Code', codex: 'Codex' }

function requestBody(values: FieldValues, codexOn: boolean): object {
  return { tool: codexOn && values.tool === TOOLS.codex ? TOOLS.codex : TOOLS.claude,
    cwd: String(values.cwd), name: String(values.name), ...(codexOn ? { prompt: String(values.prompt || '') } : {}) }
}

function creationForm(ctx: ActionContext, folders: NewSessionFolder[], codexOn: boolean, options: NewSessionOptions): FormOptions {
  const usable = folders.filter((folder) => !folder.blocked || (codexOn && !folder.codex_blocked))
  const blocked = folders.filter((folder) => folder.blocked)
  // One line of explanation; the rest sits with the field it explains.
  const ready = ctx.payload()?.notifications ? " You'll get a notification when it's ready." : ''
  const body = [codexOn
    ? `Claude Code starts with Remote Control on and is kept running.${ready} Codex starts working on its first message at once, without asking for approvals.`
    : `Starts with Remote Control on and is kept running.${ready}`]
  const notOffered = blocked.length
    ? `${codexOn ? 'Not offered to Claude Code' : 'Not offered'}: ${blocked.map((folder) => `${folder.project} (${folder.blocked})`).join('; ')}`
    : undefined
  const fields: FormField[] = [
    { kind: 'select', key: 'cwd', label: 'Folder', description: notOffered,
      value: usable.find((folder) => folder.project === options.preferredFolder || folder.cwd === options.preferredFolder)?.cwd || usable[0]?.cwd,
      options: usable.map((folder) => ({ value: folder.cwd, label: `${folder.project}  (${plural(folder.sessions, 'session')})` })) },
    { kind: 'text', key: 'name', label: 'Name', required: true, autofocus: true, maxLength: MAX_NAME_LENGTH, placeholder: 'What is this session for?' },
  ]
  if (codexOn) {
    fields.unshift({ kind: 'select', key: 'tool', label: 'Tool', value: options.tool === TOOLS.codex ? TOOLS.codex : TOOLS.claude,
      options: Object.entries(TOOL_NAME).map(([value, label]) => ({ value, label })) })
    fields.push({ kind: 'textarea', key: 'prompt', label: 'First message', placeholder: 'What should Codex do first?',
      description: 'Codex saves a session only once it has a message.',
      required: true, maxLength: 20000, showWhen: (values) => values.tool === TOOLS.codex })
  }
  return { title: 'New session', confirmLabel: 'Create', body, fields,
    validate: (values) => {
      const tool = codexOn && values.tool === TOOLS.codex ? TOOLS.codex : TOOLS.claude
      const folder = folders.find((item) => item.cwd === values.cwd)
      const why = folder?.[tool === TOOLS.codex ? 'codex_blocked' : 'blocked']
      if (why) return `${TOOL_NAME[tool]} can't start here: ${why}`
      const duplicate = nameTaken(ctx.rows(), String(values.cwd), String(values.name))
      if (duplicate) return duplicate
      // Include names, paths, JSON escaping and multibyte characters in the same body the client sends.
      return tool === TOOLS.codex && new TextEncoder().encode(JSON.stringify(requestBody(values, codexOn))).byteLength > MAX_BODY_BYTES
        ? 'request body too large' : ''
    } }
}

export async function newSession(ctx: ActionContext, options: NewSessionOptions = {}): Promise<void> {
  let folders: NewSessionFolder[]
  try { ({ folders } = await ctx.get<{ folders: NewSessionFolder[] }>('/api/folders')) }
  catch (error) { ctx.ui.toast({ message: `Couldn't load folders: ${(error as Error).message}`, tone: 'error' }); return }
  const codex = ctx.payload()?.codex
  const codexOn = Boolean(codex?.enabled && !codex.error)
  const form = creationForm(ctx, folders, codexOn, options)
  if (!folders.some((folder) => !folder.blocked || (codexOn && !folder.codex_blocked))) {
    // With no form to hang them on, each folder's reason gets its own line.
    const reasons = folders.map((folder) => `${folder.project}: ${folder.blocked}`)
    await ctx.ui.alert({ title: 'No folder is available', body: [...(form.body ?? []), ...reasons] }); return
  }
  const entered = await ctx.ui.form(form)
  if (!entered) return
  let created: CreatedSession
  try { created = await ctx.post<CreatedSession>('/api/sessions/new', requestBody(entered, codexOn)) }
  catch (error) { ctx.ui.toast({ message: `Couldn't create it: ${(error as Error).message}`, tone: 'error' }); return }
  if (codexOn && entered.tool === TOOLS.codex) {
    ctx.ui.toast({ message: `Started “${created.title}” in Codex; it's working on your first message.`, durationMs: 15000 })
  } else {
    ctx.watching.set(created.id, created.title)
    ctx.ui.toast({ message: `Starting “${created.title}”…`, durationMs: 30000 })
  }
  await ctx.refresh()
  ctx.focus(created.id)
}
