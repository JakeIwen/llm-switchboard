import type { SessionRow } from '../../api/sessionRow'
import type { FolderSettings } from '../../api/sessionsPayload'

// Placeholder filters for the prototype; the ported modules (search/, folders/,
// sessions/sort.ts) replace these.
export type StatusFilter = 'all' | 'kept' | 'open'
export type ToolFilter = 'all' | 'claude' | 'codex'
export type SortKey = 'kept' | 'recent' | 'oldest' | 'name' | 'folder' | 'size'

export const STATUS_LABELS: Record<StatusFilter, string> = { all: 'All sessions', kept: 'Kept running', open: 'Open now' }
export const TOOL_LABELS: Record<ToolFilter, string> = { all: 'All tools', claude: 'Claude', codex: 'Codex' }
export const SORT_LABELS: Record<SortKey, string> = {
  kept: 'Kept first',
  recent: 'Recent activity',
  oldest: 'Oldest activity',
  name: 'Name A–Z',
  folder: 'Folder, then recent',
  size: 'Largest first',
}

export const dirKey = (host: string, project: string) => (host ? `${host}:${project}` : project)

export interface FolderChoice {
  key: string
  host: string
  project: string
  count: number
}

export function folderChoices(rows: SessionRow[], matching: SessionRow[], opts: FolderSettings): FolderChoice[] {
  const all = new Map<string, FolderChoice>()
  for (const r of rows) {
    const key = dirKey(r.host, r.project)
    const f = all.get(key) ?? { key, host: r.host, project: r.project, count: 0 }
    f.count++
    all.set(key, f)
  }
  const listed = [...all.values()].filter((f) => {
    const m = /^~(\/.*)?$/.exec(f.project)
    if (!m || f.count < opts.min_sessions) return false
    if (opts.hidden.some((p) => f.project === p || f.project.startsWith(p + '/'))) return false
    return (m[1] ?? '').split('/').filter(Boolean).length <= opts.depth
  })
  const counts = new Map<string, number>()
  for (const r of matching) counts.set(dirKey(r.host, r.project), (counts.get(dirKey(r.host, r.project)) ?? 0) + 1)
  return listed
    .map((f) => ({ ...f, count: counts.get(f.key) ?? 0 }))
    .sort((a, b) => Number(a.host !== '') - Number(b.host !== '') || a.host.localeCompare(b.host) || a.project.localeCompare(b.project))
}

export function filterRows(rows: SessionRow[], query: string, status: StatusFilter, tool: ToolFilter): SessionRow[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  return rows.filter((r) => {
    if (status === 'kept' && !r.enabled) return false
    if (status === 'open' && !(r.open_elsewhere.length || r.status?.window_id || (r.host && r.state !== 'closed'))) return false
    if (tool !== 'all' && r.tool !== tool) return false
    const hay = `${r.title} ${r.first_prompt} ${r.project} ${r.id}`.toLowerCase()
    return words.every((w) => hay.includes(w))
  })
}

export function sortRows(rows: SessionRow[], key: SortKey): SessionRow[] {
  const recent = (a: SessionRow, b: SessionRow) => (b.mtime || 0) - (a.mtime || 0)
  const cmp: Record<SortKey, (a: SessionRow, b: SessionRow) => number> = {
    kept: (a, b) => Number(b.enabled) - Number(a.enabled) || recent(a, b),
    recent,
    oldest: (a, b) => recent(b, a),
    name: (a, b) => a.title.localeCompare(b.title),
    folder: (a, b) => a.project.localeCompare(b.project) || recent(a, b),
    size: (a, b) => (b.size || 0) - (a.size || 0) || recent(a, b),
  }
  return [...rows].sort(cmp[key])
}
