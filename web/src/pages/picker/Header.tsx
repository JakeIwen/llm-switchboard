import { ActionIcon, Button, Title, Tooltip } from '@mantine/core'
import { IconPlus, IconSettings } from '@tabler/icons-react'
import type { SessionsPayload } from '../../api/sessionsPayload'
import { hostSummary } from '../../hosts/hostRow'
import './Header.css'

function summary(data: SessionsPayload | null): string {
  // Until the first list arrives the page body says loading, or why not.
  if (!data) return ''
  const local = data.sessions.filter((r) => !r.host)
  const kept = local.filter((r) => r.enabled).length
  const rc = local.filter((r) => r.enabled && r.status?.state === 'running').length
  return [data.host, `${kept} kept`, `${rc} with Remote Control`, hostSummary(data.hosts)].filter(Boolean).join(' · ')
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
      <p className={error ? 'picker-summary is-error' : 'picker-summary'}>{summary(data)}</p>
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
