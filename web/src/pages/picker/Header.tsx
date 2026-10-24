import { ActionIcon, Button, Title, Tooltip } from '@mantine/core'
import { IconPlus, IconSettings } from '@tabler/icons-react'
import type { SessionsPayload } from '../../api/sessionsPayload'
import './Header.css'

function seenTime(t: number): string {
  return new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

function summary(data: SessionsPayload | null, error: string): string {
  if (!data) return error ? `Can't reach the Pi: ${error}` : 'loading…'
  const local = data.sessions.filter((r) => !r.host)
  const kept = local.filter((r) => r.enabled).length
  const rc = local.filter((r) => r.enabled && r.status?.rc_active).length
  const parts = [data.host, `${kept} kept`, `${rc} with Remote Control`]
  for (const h of data.hosts) parts.push(`${h.host}: ${h.online ? 'online' : `offline since ${seenTime(h.last_seen)}`}`)
  return parts.join(' · ')
}

interface Props {
  data: SessionsPayload | null
  error: string
  onNew: () => void
  onSettings: () => void
}

export function Header({ data, error, onNew, onSettings }: Props) {
  return (
    <header className="picker-header">
      <Title order={1} className="picker-title">
        LLM Switchboard
      </Title>
      <p className={error ? 'picker-summary is-error' : 'picker-summary'}>{summary(data, error)}</p>
      <div className="picker-header-actions">
        <Tooltip label="New session (n)">
          <Button variant="light" leftSection={<IconPlus size={14} />} onClick={onNew}>
            New
          </Button>
        </Tooltip>
        <Tooltip label="Settings">
          <ActionIcon variant="default" aria-label="Settings" onClick={onSettings}>
            <IconSettings size={16} />
          </ActionIcon>
        </Tooltip>
      </div>
    </header>
  )
}
