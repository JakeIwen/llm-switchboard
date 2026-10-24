import type { SessionRow } from '../api/sessionRow'

export const CHIP_DEPTH = 2
export const MIN_SESSIONS = 2
export const UNKNOWN_DIR = '(unknown folder)'
const HIDDEN_FOLDERS: readonly string[] = []
const UNDER_HOME = /^~(\/.*)?$/
export interface FolderOptions { depth?: number; minSessions?: number; hidden?: readonly string[] }
export interface FolderChoice { key: string; host: string; project: string; count: number; defaultListed: boolean }
export interface FolderChoices { choices: FolderChoice[]; total: number }
type FolderRow = Partial<Pick<SessionRow, 'host' | 'project'>>

export function listed(project: string | null | undefined, sessions: number,
  { depth = CHIP_DEPTH, minSessions = MIN_SESSIONS, hidden = HIDDEN_FOLDERS }: FolderOptions = {},
): boolean {
  const match = UNDER_HOME.exec(project || '')
  if (!match || sessions < minSessions || hidden.some((path) => project === path || project?.startsWith(path + '/'))) return false
  return (match[1] || '').split('/').filter(Boolean).length <= depth
}
export const dirKey = (host: SessionRow['host'] | undefined, project: SessionRow['project']): string =>
  host ? `${host}:${project}` : project

/** Eligibility uses all sessions; counts use other filters. Keep selected exceptions removable. */
export function folderChoices(sessions: readonly FolderRow[], selected: Iterable<string> = [],
  options: FolderOptions = {}, matching: readonly FolderRow[] = sessions,
): FolderChoices {
  const picked = new Set(selected), folders = new Map<string, FolderChoice>()
  for (const session of sessions) {
    const host = session.host || '', project = session.project || UNKNOWN_DIR
    const key = dirKey(host, project)
    const folder = folders.get(key) || { key, host, project, count: 0, defaultListed: false }
    folder.count++
    folders.set(key, folder)
  }
  for (const folder of folders.values()) {
    folder.defaultListed = listed(folder.project, folder.count, options)
    folder.count = 0
  }
  for (const session of matching) {
    const folder = folders.get(dirKey(session.host, session.project || UNKNOWN_DIR))
    if (folder) folder.count++
  }
  for (const key of picked) {
    if (folders.has(key)) continue
    const remote = /^([a-z0-9-]{1,32}):(.*)$/.exec(key)
    folders.set(key, { key, host: remote ? remote[1] : '', project: remote ? remote[2] : key, count: 0, defaultListed: false })
  }
  const choices = [...folders.values()].filter((folder) => folder.defaultListed || picked.has(folder.key))
  choices.sort((a, b) => Number(a.host !== '') - Number(b.host !== '')
    || a.host.localeCompare(b.host)
    || a.project.localeCompare(b.project, undefined, { sensitivity: 'base', numeric: true })
    || a.project.localeCompare(b.project))
  return { choices, total: choices.reduce((sum, folder) => sum + folder.count, 0) }
}

export const deviceClass = (host: SessionRow['host'] | undefined): string => host ? 'dev-host' : 'dev-local'
export const deviceTitle = (host: SessionRow['host'] | undefined): string => host ? `On ${host}` : 'On this Pi'
