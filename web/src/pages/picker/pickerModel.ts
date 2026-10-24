import type { SessionRow } from '../../api/sessionRow'
import { dirKey } from '../../folders/folderChoices'
import { compileQuery } from '../../search/sessionQuery'
import { SORTS, type SortKey } from '../../sessions/sort'

// The page's own filter vocabulary on top of the ported modules: the Show menu
// (status and tool) and the remembered tool and sort choices.
export type StatusFilter = 'all' | 'kept' | 'open'
export type ToolFilter = 'all' | 'claude' | 'codex'

export const STATUS_LABELS: Record<StatusFilter, string> = { all: 'All sessions', kept: 'Kept running', open: 'Open now' }
export const TOOL_LABELS: Record<ToolFilter, string> = { all: 'All tools', claude: 'Claude', codex: 'Codex' }
export const SORT_LABELS = Object.fromEntries(SORTS.map((s) => [s.key, s.label])) as Record<SortKey, string>
/** The sort button's value on a phone, where the full label doesn't fit. */
export const SORT_SHORT: Record<SortKey, string> = {
  kept: 'Kept',
  recent: 'Recent',
  oldest: 'Oldest',
  name: 'A–Z',
  folder: 'Folder',
  size: 'Size',
}

const STATUS_TEST: Record<StatusFilter, (row: SessionRow) => boolean> = {
  all: () => true,
  kept: (row) => row.enabled,
  open: (row) => row.enabled || row.open_elsewhere.length > 0,
}

/** Rows passing the query, status and tool, before folders and sort. */
export function matchingRows(rows: readonly SessionRow[], query: string, status: StatusFilter, tool: ToolFilter, now: number) {
  const compiled = compileQuery(query)
  const rowsOut = rows.filter(
    (row) => STATUS_TEST[status](row) && (tool === 'all' || row.tool === tool) && compiled.test(row, now),
  )
  return { rows: rowsOut, errors: compiled.errors }
}

export const inFolders = (rows: readonly SessionRow[], picked: ReadonlySet<string>) =>
  picked.size ? rows.filter((row) => picked.has(dirKey(row.host, row.project))) : [...rows]

/** The tool filter applies when Codex is on here or any row (a Mac one, say) is Codex. */
export const toolFilterApplies = (codexEnabled: boolean, rows: readonly SessionRow[]) =>
  codexEnabled || rows.some((row) => row.tool === 'codex')

// Remembered per browser, under the old page's keys so choices carry over.
const TOOL_KEY = 'switchboard.tool'
const SORT_KEY = 'switchboard.sort'

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Private mode or storage off: the choice lasts for this visit only.
  }
}

export function savedTool(): ToolFilter {
  const value = read(TOOL_KEY)
  return value === 'claude' || value === 'codex' ? value : 'all'
}

export function savedSort(): SortKey {
  const value = read(SORT_KEY)
  return SORTS.find((s) => s.key === value)?.key ?? SORTS[0].key
}

export const saveTool = (tool: ToolFilter) => write(TOOL_KEY, tool)
export const saveSort = (sort: SortKey) => write(SORT_KEY, sort)
