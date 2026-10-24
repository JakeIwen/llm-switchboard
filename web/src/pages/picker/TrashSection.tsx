import { Button, Tooltip } from '@mantine/core'
import type { TrashItem } from '../../api/sessionsPayload'
import { age, plural } from '../../format/format'
import { hostLock } from '../../hosts/hostRow'
import './TrashSection.css'

/** A Mac entry is that machine's session row; a Pi entry has no host. */
const macRow = (t: TrashItem) => ('host' in t && t.host ? t : null)

interface Props {
  items: readonly TrashItem[]
  now: number
  onRestore: (item: TrashItem) => void
  onEmpty: () => void
}

/** Recently deleted, at the end of the list: Restore, or empty the Pi's trash. */
export function TrashSection({ items, now, onRestore, onEmpty }: Props) {
  if (items.length === 0) return null
  const local = items.filter((t) => !macRow(t)).length
  return (
    <details className="trash">
      <summary className="trash-summary">Recently deleted ({items.length}) · kept 30 days</summary>
      <div className="trash-rows">
        {items.map((t) => {
          const mac = macRow(t)
          const lock = mac ? hostLock(mac) : ''
          return (
            <div key={t.id} className="trash-row">
              <div className="trash-main">
                <div className="trash-title">{t.title || '(untitled)'}</div>
                <div className="trash-meta">
                  <span className={mac ? 'trash-folder is-host' : 'trash-folder'}>{t.project}</span>
                  <span>deleted {age(t.deleted_at, now)}</span>
                  <span>
                    {mac ? 'Mac' : 'Pi'} · {t.tool === 'codex' ? 'Codex' : 'Claude'}
                  </span>
                </div>
              </div>
              <Tooltip label={lock} disabled={!lock}>
                <Button disabled={Boolean(lock)} onClick={() => onRestore(t)}>
                  Restore
                </Button>
              </Tooltip>
            </div>
          )
        })}
      </div>
      <div className="trash-foot">
        <Tooltip label="Mac trash is managed on the Mac" disabled={local > 0}>
          <Button color="var(--bad)" variant="subtle" disabled={local === 0} onClick={onEmpty}>
            Empty Pi trash now{local ? ` (${plural(local, 'session')})` : ''}
          </Button>
        </Tooltip>
      </div>
    </details>
  )
}
