import { useState, type ReactNode } from 'react'
import { ActionIcon, Button, Code, Group, Menu, Text, Tooltip } from '@mantine/core'
import { IconChevronDown, IconCopy, IconExternalLink, IconInfoCircle, IconX } from '@tabler/icons-react'
import { getJson } from '../../api/client'
import type { SessionRow } from '../../api/sessionRow'
import { copyText } from '../../feedback/copyText'
import { age, size } from '../../format/format'
import { hostLock } from '../../hosts/hostRow'
import { sessionBlockedReason } from '../../sessions/badges'
import './SessionDetails.css'

interface Message {
  role: string
  text: string
}

/** What the row's buttons do; PickerPage wires them to the action flows. */
export interface RowActions {
  rename: (row: SessionRow) => void
  remoteControlName: (row: SessionRow) => void
  move: (row: SessionRow) => void
  fork: (row: SessionRow) => void
  forkToPi: (row: SessionRow) => void
  restart: (row: SessionRow) => void
  remove: (row: SessionRow) => void
  copied: (ok: boolean) => void
}

// The Mac's rule for what leaves it, behind an info icon rather than on the page.
const MAC_NOTE =
  'Actions are queued for the Mac and expire after 60 seconds; it checks each one itself. Forks never start running. Messages and terminal screens stay on the Mac, except a transcript you fork onto the Pi.'

function CommandLine({ label, command, onCopied }: { label: string; command: string; onCopied: (ok: boolean) => void }) {
  return (
    <div className="details-command">
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <div className="details-command-line">
        <Code className="details-command-text">{command}</Code>
        <Tooltip label="Copy">
          <ActionIcon aria-label={`Copy: ${label}`} onClick={() => void copyText(command).then(onCopied)}>
            <IconCopy size={16} />
          </ActionIcon>
        </Tooltip>
      </div>
    </div>
  )
}

/** A button that explains itself when it can't be used: the reason is its tooltip. */
function Guarded({ reason, children, onClick, color, variant, className }: {
  reason: string
  children: ReactNode
  onClick: () => void
  color?: string
  variant?: string
  className?: string
}) {
  return (
    <Tooltip label={reason} disabled={!reason}>
      <Button disabled={Boolean(reason)} onClick={onClick} color={color} variant={variant} className={className}>
        {children}
      </Button>
    </Tooltip>
  )
}

function MoveOrFork({ row, lock, moveLock, actions }: { row: SessionRow; lock: string; moveLock: string; actions: RowActions }) {
  const remote = Boolean(row.host)
  return (
    <Button.Group>
      <Guarded reason={moveLock} onClick={() => actions.move(row)}>
        Move…
      </Guarded>
      <Menu position="bottom-end">
        <Menu.Target>
          <Button disabled={Boolean(lock)} px={6} aria-label="More folder actions">
            <IconChevronDown size={14} />
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item onClick={() => actions.fork(row)}>
            {remote ? 'Fork on the Mac…' : 'Fork…'}
            <Text size="xs" c="dimmed">
              Copy the conversation into a new session; the original stays as it is
            </Text>
          </Menu.Item>
          {remote && row.tool === 'claude' && (
            <Menu.Item onClick={() => actions.forkToPi(row)}>
              Fork onto the Pi…
              <Text size="xs" c="dimmed">
                Uploads this one transcript to the Pi as a new, not running session
              </Text>
            </Menu.Item>
          )}
        </Menu.Dropdown>
      </Menu>
    </Button.Group>
  )
}

/** The expanded part of a row: actions first, then facts, Delete last. */
export function SessionDetails({ row, actions }: { row: SessionRow; actions: RowActions }) {
  const [messages, setMessages] = useState<Message[] | string | null>(null)
  const [screen, setScreen] = useState<string | null>(null)
  const remote = Boolean(row.host)
  const codex = row.tool === 'codex'
  const piClaude = !remote && !codex
  const kept = piClaude && row.enabled
  // Mac rows: one lock for every change. Pi Claude: open elsewhere blocks
  // rename, move and delete, but not fork or the Remote Control name.
  const lock = remote ? hostLock(row) : ''
  const blocked = remote ? lock : piClaude ? sessionBlockedReason(row) : ''
  const exit = row.status?.last_exit

  const loadMessages = () =>
    void getJson<{ messages: Message[] }>(`/api/sessions/${row.id}/messages`)
      .then((d) => setMessages(d.messages))
      .catch((e: Error) => setMessages(`Couldn't load messages: ${e.message}`))
  const loadScreen = () =>
    void getJson<{ screen: string }>(`/api/sessions/${row.id}/screen`)
      .then((d) => setScreen(d.screen || '(nothing to show)'))
      .catch((e: Error) => setScreen(`error: ${e.message}`))

  return (
    <div className="session-details">
      <Group gap="xs" className="details-actions">
        {row.remote_url && (
          <Button component="a" href={row.remote_url} target="_blank" rel="noopener" variant="light" rightSection={<IconExternalLink size={14} />}>
            Open in Claude
          </Button>
        )}
        {!remote && <Button onClick={loadMessages}>{messages === null ? 'Recent messages' : 'Refresh messages'}</Button>}
        {kept && <Button onClick={loadScreen}>{screen === null ? 'Terminal screen' : 'Refresh screen'}</Button>}
        {kept && <Button onClick={() => actions.restart(row)}>Restart</Button>}
        <Guarded reason={blocked} onClick={() => actions.rename(row)}>
          Rename…
        </Guarded>
        {piClaude && <Button onClick={() => actions.remoteControlName(row)}>Remote Control name…</Button>}
        {codex ? (
          <Guarded reason={lock} onClick={() => actions.fork(row)}>
            {remote ? 'Fork on the Mac…' : 'Fork…'}
          </Guarded>
        ) : (
          <MoveOrFork row={row} lock={lock} moveLock={blocked} actions={actions} />
        )}
        <Guarded reason={blocked} color="var(--bad)" variant="subtle" className="details-delete" onClick={() => actions.remove(row)}>
          Delete
        </Guarded>
        {remote && (
          <Tooltip label={MAC_NOTE}>
            <ActionIcon aria-label="About Mac actions">
              <IconInfoCircle size={16} />
            </ActionIcon>
          </Tooltip>
        )}
      </Group>
      {blocked && <Text size="sm" c="dimmed">{blocked}</Text>}
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
      {exit && (
        <Text size="sm" c="dimmed">
          Last exit: status {exit.status ?? '?'} after {Math.round(exit.ran_s)}s, {age(exit.at)} · launches: {row.status?.launches}
        </Text>
      )}
      <CommandLine label={remote ? 'Resume in a shell on the Mac' : 'Resume in a shell on the Pi'} command={row.resume_cmd} onCopied={actions.copied} />
      {kept && (
        <CommandLine
          label="Attach to its tmux window (detach: Ctrl-b d)"
          command={`llm-switchboard attach ${row.short_id}`}
          onCopied={actions.copied}
        />
      )}
      <Text size="xs" c="dimmed" className="details-id">
        <span className="details-mono">{row.id}</span>
        {row.size ? ` · transcript ${size(row.size)}` : ''}
        {piClaude && row.rc_name ? ` · Remote Control name “${row.rc_name}”` : ''}
      </Text>
    </div>
  )
}
