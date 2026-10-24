import { Button, CloseButton, Menu } from '@mantine/core'
import { IconDots } from '@tabler/icons-react'
import './BulkBar.css'

export type BulkAction = 'keep' | 'stop' | 'move' | 'delete' | 'all' | 'clear'

interface Props {
  count: number
  progress: string | null
  onAction: (action: BulkAction) => void
}

/** The bar for a selection, within thumb reach at the bottom of the screen. */
export function BulkBar({ count, progress, onAction }: Props) {
  if (count === 0 && !progress) return null
  const busy = progress !== null
  return (
    <div className="bulkbar" role="toolbar" aria-label="Selected sessions">
      <span className="bulkbar-count">{progress ?? `${count} selected`}</span>
      <div className="bulkbar-actions">
        <Button disabled={busy} onClick={() => onAction('keep')}>
          Keep running
        </Button>
        <Button disabled={busy} className="bulkbar-wide" onClick={() => onAction('stop')}>
          Stop keeping
        </Button>
        <Button disabled={busy} className="bulkbar-wide" onClick={() => onAction('move')}>
          Move…
        </Button>
        <Button disabled={busy} color="var(--bad)" variant="light" onClick={() => onAction('delete')}>
          Delete
        </Button>
        <Menu position="top-end">
          <Menu.Target>
            <Button disabled={busy} aria-label="More selection actions" px={6}>
              <IconDots size={16} />
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item className="bulkbar-narrow" onClick={() => onAction('stop')}>
              Stop keeping
            </Menu.Item>
            <Menu.Item className="bulkbar-narrow" onClick={() => onAction('move')}>
              Move to folder…
            </Menu.Item>
            <Menu.Item onClick={() => onAction('all')}>Select all shown</Menu.Item>
          </Menu.Dropdown>
        </Menu>
        <CloseButton aria-label="Clear selection" disabled={busy} onClick={() => onAction('clear')} />
      </div>
    </div>
  )
}
