import { useMemo, useState, type ReactElement } from 'react'
import { Button, Chip, Drawer, Popover, TextInput } from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { isMachineKey, type FolderChoice, type MachineChoice } from '../../folders/folderChoices'
import { groupChoices, visibleChoices } from '../../folders/folderSelection'
import { plural } from '../../format/format'
import './FolderPicker.css'

interface Props {
  choices: FolderChoice[]
  machines: MachineChoice[]
  selected: string[]
  hostLabel: string
  onChange: (keys: string[]) => void
}

/** A folder's chip text: home-relative paths without the `~/` every one shares. */
const chipText = (project: string) => (project.startsWith('~/') ? project.slice(2) : project)

/** A place is a folder or a whole machine; machine keys are "@host". */
const machineHost = (key: string) => key.slice(1)

export function folderTriggerLabel(selected: string[], choices: FolderChoice[], hostLabel: string): string {
  if (selected.length === 0) return 'All places'
  if (selected.length > 1) return `${selected.length} places`
  const [key] = selected
  if (isMachineKey(key)) return machineHost(key) || hostLabel
  return chipText(choices.find((c) => c.key === key)?.project ?? key)
}

/** The one picked place's machine, for the trigger's tint; '' for this Pi. */
export const pickedHost = (selected: string[], choices: FolderChoice[]): string | null => {
  if (selected.length !== 1) return null
  const [key] = selected
  return isMachineKey(key) ? machineHost(key) : (choices.find((c) => c.key === key)?.host ?? null)
}

function FolderChips({ choices, machines, selected, hostLabel, onChange, onDone }: Props & { onDone: () => void }) {
  const [find, setFind] = useState('')
  const visible = useMemo(() => visibleChoices(choices, find), [choices, find])
  const groups = useMemo(() => {
    const folders = groupChoices(visible)
    const query = find.trim().toLocaleLowerCase()
    const named = machines.filter((m) => folders.has(m.host) || (m.host || hostLabel).toLocaleLowerCase().includes(query))
    // A picked folder can outlast its machine's place (no matches): keep its group.
    const hosts = [...new Set([...named.map((m) => m.host), ...folders.keys()])]
    return hosts.map((host) => ({ host, machine: machines.find((m) => m.host === host), folders: folders.get(host) ?? [] }))
  }, [visible, machines, find, hostLabel])
  const invert = () => {
    const shown = new Set(visible.map((c) => c.key))
    const kept = selected.filter((k) => !shown.has(k))
    onChange([...kept, ...visible.filter((c) => !selected.includes(c.key)).map((c) => c.key)])
  }
  return (
    <div className="folder-picker">
      <TextInput
        type="search"
        placeholder="Find a place"
        aria-label="Find a place"
        value={find}
        onChange={(e) => setFind(e.currentTarget.value)}
      />
      <div className="folder-scroll">
        <Chip.Group multiple value={selected} onChange={onChange}>
          {groups.map(({ host, machine, folders }) => (
            <section key={host || 'local'} className="folder-group">
              <div className={host ? 'folder-group-name is-host' : 'folder-group-name'}>
                {machine ? (
                  <Chip value={machine.key} size="sm" variant="light" classNames={{ label: 'folder-machine-label' }} title={`Every session on ${host || hostLabel}`}>
                    {host || hostLabel}
                    <span className="folder-chip-count">{machine.count}</span>
                  </Chip>
                ) : (
                  <h3 className="folder-group-heading">{host || hostLabel}</h3>
                )}
              </div>
              <div className="folder-chips">
                {folders.map((f) => (
                  <Chip key={f.key} value={f.key} size="sm" variant="light" title={f.project}>
                    {chipText(f.project)}
                    {!f.defaultListed && <span className="folder-chip-note"> · outside default folders</span>}
                    <span className="folder-chip-count">{f.count}</span>
                  </Chip>
                ))}
              </div>
            </section>
          ))}
        </Chip.Group>
        {groups.length === 0 && <p className="folder-empty">No places match your search.</p>}
      </div>
      <div className="folder-foot">
        <Button disabled={selected.length === 0} onClick={() => onChange([])}>
          Clear
        </Button>
        <Button disabled={visible.length === 0} onClick={invert}>
          Invert
        </Button>
        <span className="folder-foot-status">
          {selected.length ? `${plural(selected.length, 'place')} selected` : 'All places shown'}
        </span>
        <Button variant="light" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  )
}

/** The place filter (folders, and whole machines): a bottom sheet on a phone, a popover on a wider screen.
    The caller owns the trigger button and the open state. */
export function FolderPicker({
  target,
  opened,
  onOpenChange,
  ...props
}: Props & { target: ReactElement; opened: boolean; onOpenChange: (opened: boolean) => void }) {
  const phone = useMediaQuery('(max-width: 47.99em)')
  const chips = <FolderChips {...props} onDone={() => onOpenChange(false)} />
  if (phone) {
    return (
      <>
        {target}
        <Drawer
          opened={opened}
          onClose={() => onOpenChange(false)}
          position="bottom"
          size="auto"
          title="Places"
          classNames={{ content: 'folder-sheet', body: 'folder-sheet-body' }}
          // Focus the close button, not the find box, so the phone keyboard stays down.
          closeButtonProps={{ 'aria-label': 'Close places', 'data-autofocus': true } as Record<string, unknown>}
        >
          {chips}
        </Drawer>
      </>
    )
  }
  return (
    <Popover opened={opened} onChange={onOpenChange} width={560} position="bottom-start" trapFocus>
      <Popover.Target>{target}</Popover.Target>
      <Popover.Dropdown>{chips}</Popover.Dropdown>
    </Popover>
  )
}
