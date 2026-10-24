import { useState } from 'react'
import { ActionIcon, Button, Code, Group, Text, Tooltip } from '@mantine/core'
import { IconCopy, IconExternalLink, IconX } from '@tabler/icons-react'
import { getJson } from '../../api/client'
import type { SessionRow } from '../../api/sessionRow'
import { copyText } from '../../feedback/copyText'
import { size } from '../../format/format'
import './SessionDetails.css'

interface Message {
  role: string
  text: string
}

function CommandLine({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="details-command">
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <div className="details-command-line">
        <Code className="details-command-text">{command}</Code>
        <Tooltip label={copied ? 'Copied' : 'Copy'}>
          <ActionIcon
            aria-label={`Copy: ${label}`}
            onClick={() => void copyText(command).then((ok) => setCopied(ok))}
          >
            <IconCopy size={16} />
          </ActionIcon>
        </Tooltip>
      </div>
    </div>
  )
}

/** The expanded part of a row: actions first, then facts. */
export function SessionDetails({ row }: { row: SessionRow }) {
  const [messages, setMessages] = useState<Message[] | string | null>(null)
  const [screen, setScreen] = useState<string | null>(null)
  const local = !row.host
  const claudeLocal = local && row.tool === 'claude'

  const loadMessages = () =>
    void getJson<{ messages: Message[] }>(`/api/sessions/${row.id}/messages`)
      .then((d) => setMessages(d.messages))
      .catch((e: Error) => setMessages(e.message))
  const loadScreen = () =>
    void getJson<{ screen: string }>(`/api/sessions/${row.id}/screen`)
      .then((d) => setScreen(d.screen || '(nothing to show)'))
      .catch((e: Error) => setScreen(e.message))

  return (
    <div className="session-details">
      <Group gap="xs" className="details-actions">
        {row.remote_url && (
          <Button component="a" href={row.remote_url} target="_blank" rel="noopener" variant="light" rightSection={<IconExternalLink size={14} />}>
            Open in Claude
          </Button>
        )}
        {local && <Button onClick={loadMessages}>{messages === null ? 'Recent messages' : 'Refresh messages'}</Button>}
        {claudeLocal && row.enabled && <Button onClick={loadScreen}>{screen === null ? 'Terminal screen' : 'Refresh screen'}</Button>}
        {claudeLocal && row.enabled && <Button>Restart</Button>}
        <Button>Rename…</Button>
        <Button>{row.tool === 'codex' ? 'Fork…' : 'Move or fork…'}</Button>
        <Button color="red" variant="subtle">
          Delete
        </Button>
      </Group>
      {messages !== null && (
        <div className="details-messages">
          {typeof messages === 'string' ? (
            <Text size="sm" c="dimmed">
              {messages}
            </Text>
          ) : messages.length === 0 ? (
            <Text size="sm" c="dimmed">
              No messages yet.
            </Text>
          ) : (
            messages.map((m, i) => (
              <div key={i} className={m.role === 'user' ? 'details-message is-user' : 'details-message'}>
                {m.text}
              </div>
            ))
          )}
        </div>
      )}
      {screen !== null && (
        <div className="details-screen">
          <pre>{screen}</pre>
          <ActionIcon className="details-screen-close" aria-label="Close terminal screen" onClick={() => setScreen(null)}>
            <IconX size={14} />
          </ActionIcon>
        </div>
      )}
      {row.enabled && row.status?.detail && row.status.state !== 'running' && (
        <Text size="sm" c="dimmed">
          {row.status.detail}
        </Text>
      )}
      <CommandLine label={local ? 'Resume in a shell on the Pi' : `Resume on ${row.host}`} command={row.resume_cmd} />
      {claudeLocal && row.enabled && (
        <CommandLine label="Attach to its tmux window (detach: Ctrl-b d)" command={`llm-switchboard attach ${row.short_id}`} />
      )}
      <Text size="xs" c="dimmed" className="details-id">
        <span className="details-mono">{row.id}</span>
        {row.size ? ` · transcript ${size(row.size)}` : ''}
      </Text>
    </div>
  )
}
