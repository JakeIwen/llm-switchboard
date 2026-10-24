import { Button } from '@mantine/core'
import { IconFileSearch } from '@tabler/icons-react'
import type { SessionRow as Row } from '../../api/sessionRow'
import { plural } from '../../format/format'
import { SessionRow, type SelectEvent } from './SessionRow'
import './SessionList.css'

export interface RowHandlers {
  onExpand: (id: string) => void
  onSelect: (id: string, event: SelectEvent) => void
  onKeep: (row: Row, keep: boolean) => void
  onFolder: (row: Row) => void
}

interface Props extends RowHandlers {
  rows: Row[]
  total: number
  query: string
  showSize: boolean
  filtered: boolean
  expanded: ReadonlySet<string>
  selected: ReadonlySet<string>
  now: number
  onSearchTranscripts: () => void
  onReset: () => void
}

/** Every row renders (no virtual list), so find-in-page works; unchanged rows
    are memoized and skip each poll's render. */
export function SessionList(p: Props) {
  const query = p.query.trim()
  return (
    <div className="session-list">
      <div className="session-list-head">
        {query ? (
          <Button
            className="transcript-offer"
            variant="subtle"
            justify="flex-start"
            leftSection={<IconFileSearch size={16} />}
            classNames={{ label: 'transcript-offer-inner' }}
            onClick={p.onSearchTranscripts}
          >
            <span className="transcript-offer-label">Search inside transcripts for “{query}”</span>
            <kbd className="transcript-offer-key">Enter</kbd>
          </Button>
        ) : (
          <span className="session-list-hint">
            {p.rows.length === p.total ? plural(p.total, 'session') : `${p.rows.length} of ${plural(p.total, 'session')}`}
          </span>
        )}
      </div>
      <div className="session-list-rows">
        {p.rows.map((row) => (
          <SessionRow
            key={row.id}
            row={row}
            expanded={p.expanded.has(row.id)}
            selected={p.selected.has(row.id)}
            now={p.now}
            showSize={p.showSize}
            onExpand={p.onExpand}
            onSelect={p.onSelect}
            onKeep={p.onKeep}
            onFolder={p.onFolder}
          />
        ))}
        {p.rows.length === 0 && p.total > 0 && (
          <div className="session-list-empty">
            <p>No session matches{query ? ` “${query}”` : ''}{p.filtered ? ' with these filters' : ''}.</p>
            {p.filtered && <Button onClick={p.onReset}>Reset filters</Button>}
          </div>
        )}
      </div>
    </div>
  )
}
