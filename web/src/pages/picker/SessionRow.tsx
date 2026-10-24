import { memo, type ReactNode } from 'react'
import { Badge, Checkbox, Switch, Tooltip } from '@mantine/core'
import type { SessionRow as Row } from '../../api/sessionRow'
import { age, size } from '../../format/format'
import { rowBadges, toneClass } from './rowBadges'
import { SessionDetails } from './SessionDetails'
import './SessionRow.css'

export interface SelectEvent {
  shiftKey: boolean
  metaKey: boolean
  ctrlKey: boolean
}

interface Props {
  row: Row
  expanded: boolean
  selected: boolean
  now: number
  onExpand: (id: string) => void
  onSelect: (id: string, event: SelectEvent) => void
  onKeep: (row: Row, keep: boolean) => void
  onFolder: (row: Row) => void
  /** Show the transcript size (when sorting by size); the details always do. */
  showSize?: boolean
  /** Extra lines under the meta line (a transcript hit's excerpt). */
  extra?: ReactNode
}

// A collapsed row shows at most this many state pills; the rest are in the details.
const MAX_PILLS = 2
const keepable = (row: Row) => !row.host && row.tool === 'claude'

function IdentityTag({ row }: { row: Row }) {
  return (
    <span className="identity" title={`${row.tool === 'codex' ? 'A Codex' : 'A Claude Code'} session on ${row.host || 'this Pi'}`}>
      <span className={row.host ? 'identity-machine is-host' : 'identity-machine'}>{row.host ? 'Mac' : 'Pi'}</span>
      <span className="identity-tool">{row.tool === 'codex' ? 'Codex' : 'Claude'}</span>
    </span>
  )
}

export const SessionRow = memo(function SessionRow(p: Props) {
  const { row } = p
  const pills = rowBadges(row).slice(0, MAX_PILLS)
  const offline = Boolean(row.host) && row.host_online === false
  const classes = ['session-row', row.enabled && 'is-kept', p.selected && 'is-selected', offline && 'is-offline']
  return (
    <div className={classes.filter(Boolean).join(' ')} data-id={row.id}>
      <label className="session-hit session-check">
        <Checkbox
          aria-label={`Select ${row.title || 'untitled session'}`}
          checked={p.selected}
          onChange={() => {}}
          onClick={(e) => p.onSelect(row.id, e)}
        />
      </label>
      <div className="session-main">
        <button type="button" className="session-title" aria-expanded={p.expanded} onClick={() => p.onExpand(row.id)}>
          {row.title || '(untitled)'}
        </button>
        <div className="session-meta">
          <IdentityTag row={row} />
          <button
            type="button"
            className={row.host ? 'session-folder is-host' : 'session-folder'}
            title={`Show only ${row.project || 'this folder'}`}
            onClick={() => p.onFolder(row)}
          >
            {row.project || '(unknown folder)'}
          </button>
          <span>{age(row.mtime, p.now)}</span>
          {p.showSize && row.size ? <span>{size(row.size)}</span> : null}
          {pills.map((b) => (
            <Badge key={b.label} className={toneClass[b.tone]}>
              {b.label}
            </Badge>
          ))}
        </div>
        {p.extra}
      </div>
      {keepable(row) ? (
        <Tooltip label={row.enabled ? 'Kept running with Remote Control; switch off to stop keeping it' : 'Keep running in tmux with Remote Control'}>
          <label className="session-hit session-keep">
            <Switch aria-label="Keep running" checked={row.enabled} onChange={(e) => p.onKeep(row, e.currentTarget.checked)} />
          </label>
        </Tooltip>
      ) : (
        <span className="session-keep" />
      )}
      {p.expanded && <SessionDetails row={row} />}
    </div>
  )
})
