import type { SessionRow } from '../api/sessionRow'

export const MAX_NAME_LENGTH = 120
const FORK_TITLE_PREFIX_LENGTH = 100
const nameKey = (title: string): string => title.trim().split(/\s+/).join(' ').toLowerCase()

export const forkTitle = (title: string, number = 1): string =>
  `${title.slice(0, FORK_TITLE_PREFIX_LENGTH)} (fork${number > 1 ? ' ' + number : ''})`

/** An early explanation only: the server canonicalizes paths and remains authoritative. */
export function nameTaken(rows: readonly SessionRow[], cwd: string, title: string): string {
  const folder = cwd.trim().replace(/(.)\/+$/, '$1')
  const other = rows.find((row) => !row.host && (row.cwd === folder || row.project === folder) && nameKey(row.title) === nameKey(title))
  return other ? `${other.project} already has a session named “${other.title}”.` : ''
}

export function forkName(rows: readonly SessionRow[], row: SessionRow): string {
  for (let n = 1; ; n++) {
    const name = forkTitle(row.title, n)
    if (!nameTaken(rows, row.cwd, name)) return name
  }
}
