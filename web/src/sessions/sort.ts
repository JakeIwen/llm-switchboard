import type { SessionRow } from '../api/sessionRow'

type SortRow = Pick<SessionRow, 'title' | 'project' | 'mtime' | 'size' | 'enabled'>
type Compare = (a: SortRow, b: SortRow) => number
const recent: Compare = (a, b) => (b.mtime || 0) - (a.mtime || 0)
const byText = (field: (row: SortRow) => string): Compare => (a, b) =>
  field(a).localeCompare(field(b), undefined, { sensitivity: 'base', numeric: true })

export const SORTS = [
  { key: 'kept', label: 'Kept first', compare: (a, b) => Number(b.enabled) - Number(a.enabled) || recent(a, b) },
  { key: 'recent', label: 'Recent activity', compare: recent },
  { key: 'oldest', label: 'Oldest activity', compare: (a, b) => recent(b, a) },
  { key: 'name', label: 'Name A–Z', compare: byText((s) => s.title) },
  { key: 'folder', label: 'Folder, then recent', compare: (a, b) => byText((s) => s.project)(a, b) || recent(a, b) },
  { key: 'size', label: 'Largest first', compare: (a, b) => (b.size || 0) - (a.size || 0) || recent(a, b) },
] as const satisfies readonly { key: string; label: string; compare: Compare }[]
export type SortKey = typeof SORTS[number]['key']
export const DEFAULT_SORT: SortKey = SORTS[0].key
const SORT_BY_KEY = Object.fromEntries(SORTS.map((sort) => [sort.key, sort])) as Record<SortKey, typeof SORTS[number]>

/** Copy before sorting; equal values preserve the server's stable order. */
export function sortRows<T extends SortRow>(rows: readonly T[], key?: string): T[] {
  const sort = key && Object.hasOwn(SORT_BY_KEY, key) ? SORT_BY_KEY[key as SortKey] : SORT_BY_KEY[DEFAULT_SORT]
  return rows.map((row, i) => ({ row, i }))
    .sort((a, b) => sort.compare(a.row, b.row) || a.i - b.i).map(({ row }) => row)
}
