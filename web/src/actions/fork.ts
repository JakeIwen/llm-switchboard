import { TOOLS, type SessionRow } from '../api/sessionRow'
import { actionPath, hostLock } from '../hosts/hostRow'
import { forkName, forkTitle, MAX_NAME_LENGTH, nameTaken } from './actionNames'
import { queueHostAction } from './actionRequests'
import type { ActionContext, CreatedSession } from './context'
import type { FormField } from './ui'

export async function forkSession(ctx: ActionContext, row: SessionRow, initialCwd = row.cwd): Promise<void> {
  if (row.host) return forkOnMac(ctx, row, initialCwd)
  const codex = row.tool === TOOLS.codex
  const fields: FormField[] = [
    { kind: 'text', key: 'title', label: 'Name', required: true, autofocus: true, maxLength: MAX_NAME_LENGTH,
      value: forkName(ctx.rows(), row) },
    { kind: 'text', key: 'cwd', label: 'Folder', required: true, value: initialCwd,
      suggestions: ctx.knownFolders(), mono: true, placeholder: '/home/pi/dev/…' },
  ]
  if (!codex) fields.push({ kind: 'checkbox', key: 'keep', label: 'Keep it running with Remote Control', value: row.enabled })
  const entered = await ctx.ui.form({
    title: `Fork “${row.title}”`, confirmLabel: 'Fork', fields,
    body: [`Copies the conversation so far into a new ${codex ? 'Codex' : 'Claude Code'} session. The original is left as it is, even if it's running.`],
    validate: (values) => nameTaken(ctx.rows(), String(values.cwd), String(values.title)),
  })
  if (!entered) return
  const keep = !codex && entered.keep === true
  let made: CreatedSession
  try {
    made = await ctx.post<CreatedSession>(actionPath(row.id, 'fork'), {
      title: String(entered.title), cwd: String(entered.cwd).trim(), ...(!codex ? { keep } : {}),
    })
  } catch (error) { ctx.ui.toast({ message: `Fork failed: ${(error as Error).message}`, tone: 'error' }); return }
  if (keep) ctx.watching.set(made.id, made.title)
  await ctx.refresh()
  ctx.focus(made.id)
  ctx.ui.toast({ message: `Forked as “${made.title}”${keep ? '; starting it…' : '.'}` })
}

export async function forkOnMac(ctx: ActionContext, row: SessionRow, initialCwd = row.cwd): Promise<void> {
  if (!row.host) return
  const locked = hostLock(row)
  if (locked) { ctx.ui.toast({ message: locked }); return }
  const entered = await ctx.ui.form({
    title: 'Fork on the Mac', confirmLabel: 'Queue fork',
    body: ['Copies the conversation on the Mac. No process starts, and the copy is not kept running.'],
    fields: [
      { kind: 'text', key: 'title', label: 'Name for the copy', value: forkTitle(row.title),
        required: true, autofocus: true, maxLength: MAX_NAME_LENGTH },
      { kind: 'text', key: 'cwd', label: 'Existing folder on the Mac', value: initialCwd, required: true,
        mono: true, suggestions: ctx.knownFolders() },
    ],
  })
  if (!entered) return
  await queueHostAction(ctx, row, 'fork', { title: String(entered.title), cwd: String(entered.cwd) })
}

export async function forkOntoPi(ctx: ActionContext, row: SessionRow): Promise<void> {
  if (!row.host || row.tool === TOOLS.codex) return
  const locked = hostLock(row)
  if (locked) { ctx.ui.toast({ message: locked }); return }
  const entered = await ctx.ui.form({
    title: 'Fork onto the Pi', confirmLabel: 'Queue fork',
    body: ['Uploads this one transcript to the Pi. ~/<folder> on the Mac maps to ~/<folder> on the Pi, which must exist. The Mac posts a notification and logs the upload (10 per hour; 16 MiB maximum). The copy does not start running.'],
    fields: [{ kind: 'text', key: 'title', label: 'Name for the copy', value: forkTitle(row.title),
      required: true, autofocus: true, maxLength: MAX_NAME_LENGTH }],
  })
  if (!entered) return
  await queueHostAction(ctx, row, 'fork-pi', { title: String(entered.title) })
}
