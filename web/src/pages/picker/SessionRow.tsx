import { memo, type ReactNode } from 'react'
import { Badge, Checkbox, Switch, Tooltip } from '@mantine/core'
import type { SessionRow as Row } from '../../api/sessionRow'
import { age, size } from '../../format/format'
import { badges, type BadgeTone } from '../../sessions/badges'
import { SessionDetails, type RowActions } from './SessionDetails'
import type { RowNotice } from './useHostActions'
import type { SelectEvent } from './useSelection'
import './SessionRow.css'


interface Props {
  row: Row
  expanded: boolean
  selected: boolean
  now: number
  /** A plain click expands; ⌘/Ctrl or Shift selects instead (PickerPage decides). */
  onExpand: (id: string, event: SelectEvent) => void
  onSelect: (id: string, event: SelectEvent) => void
  onKeep: (row: Row, keep: boolean) => void
  onFolder: (row: Row) => void
  actions: RowActions
  /** A queued Mac action's state, shown as the first pill. */
  notice?: RowNotice
  /** Ahead of the keyboard cursor. */
  cursor?: boolean
  /** Show the transcript size (when sorting by size); the details always do. */
  showSize?: boolean
  /** Extra lines under the meta line (a transcript hit's excerpt). */
  extra?: ReactNode
}

// A collapsed row shows at most this many state pills; the rest are in the details.
const MAX_PILLS = 2
const TONE_CLASS: Record<BadgeTone, string> = {
  ok: 'tone-ok',
  info: 'tone-info',
  warn: 'tone-warn',
  bad: 'tone-bad',
  idle: 'tone-idle',
}

/** The row's pills. A Mac row's first badge is its host status; while the Mac
    is online the header already says so and the row isn't dimmed, so drop it. */
function pills(row: Row) {
  const all = badges(row)
  return (row.host && row.host_online ? all.slice(1) : all).slice(0, MAX_PILLS)
}
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
  const offline = Boolean(row.host) && row.host_online === false
  const shownPills = pills(row)
  const classes = ['session-row', row.enabled && 'is-kept', p.selected && 'is-selected', p.cursor && 'is-cursor', offline && 'is-offline']
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
        <button type="button" className="session-title" aria-expanded={p.expanded} onClick={(e) => p.onExpand(row.id, e)}>
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
          {(p.notice || shownPills.length > 0) && (
            <span className="session-pills">
              {p.notice && (
                <Badge className={TONE_CLASS[p.notice.tone]} title={p.notice.title}>
                  {p.notice.label}
                </Badge>
              )}
              {shownPills.map((b) => (
                <Badge key={b.label} className={TONE_CLASS[b.tone]}>
                  {b.label}
                </Badge>
              ))}
            </span>
          )}
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
      {p.expanded && <SessionDetails row={row} actions={p.actions} />}
    </div>
  )
})
