import { Button, Tooltip } from '@mantine/core'
import type { TrashItem } from '../../api/sessionsPayload'
import { age, plural } from '../../format/format'
import { hostLock } from '../../hosts/hostRow'
import './TrashSection.css'

interface Props {
  items: readonly TrashItem[]
  now: number
  onRestore: (item: TrashItem) => void
  onEmpty: () => void
}

/** Recently deleted, at the end of the list: Restore, or empty the Pi's trash. */
export function TrashSection({ items, now, onRestore, onEmpty }: Props) {
  if (items.length === 0) return null
  const local = items.filter((t) => !t.host).length
  return (
    <details className="trash">
      <summary className="trash-summary">Recently deleted ({items.length}) · kept 30 days</summary>
      <div className="trash-rows">
        {items.map((t) => {
          const lock = t.host ? hostLock({ host: t.host, host_online: t.host_online, mutable: t.mutable, state: t.state }) : ''
          return (
            <div key={t.id} className="trash-row">
              <div className="trash-main">
                <div className="trash-title">{t.title || '(untitled)'}</div>
                <div className="trash-meta">
                  <span className={t.host ? 'trash-folder is-host' : 'trash-folder'}>{t.project}</span>
                  <span>deleted {age(t.deleted_at, now)}</span>
                  <span>
                    {t.host ? 'Mac' : 'Pi'} · {t.tool === 'codex' ? 'Codex' : 'Claude'}
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
          <Button color="red" variant="subtle" disabled={local === 0} onClick={onEmpty}>
            Empty Pi trash now{local ? ` (${plural(local, 'session')})` : ''}
          </Button>
        </Tooltip>
      </div>
    </details>
  )
}
