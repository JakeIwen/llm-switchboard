import { useMemo, useState, type ReactElement } from 'react'
import { Button, Chip, Drawer, Popover, TextInput } from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import type { FolderChoice } from '../../folders/folderChoices'
import { groupChoices, visibleChoices } from '../../folders/folderSelection'
import { plural } from '../../format/format'
import './FolderPicker.css'

interface Props {
  choices: FolderChoice[]
  selected: string[]
  hostLabel: string
  onChange: (keys: string[]) => void
}

/** A folder's chip text: home-relative paths without the `~/` every one shares. */
const chipText = (project: string) => (project.startsWith('~/') ? project.slice(2) : project)

export function folderTriggerLabel(selected: string[], choices: FolderChoice[]): string {
  if (selected.length === 0) return 'All folders'
  if (selected.length > 1) return `${selected.length} folders`
  return chipText(choices.find((c) => c.key === selected[0])?.project ?? selected[0])
}

/** The one picked folder's machine, for the trigger's tint; '' for this Pi. */
export const pickedHost = (selected: string[], choices: FolderChoice[]): string | null =>
  selected.length === 1 ? (choices.find((c) => c.key === selected[0])?.host ?? null) : null

function FolderChips({ choices, selected, hostLabel, onChange, onDone }: Props & { onDone: () => void }) {
  const [find, setFind] = useState('')
  const visible = useMemo(() => visibleChoices(choices, find), [choices, find])
  const groups = useMemo(() => [...groupChoices(visible)], [visible])
  const invert = () => {
    const shown = new Set(visible.map((c) => c.key))
    const kept = selected.filter((k) => !shown.has(k))
    onChange([...kept, ...visible.filter((c) => !selected.includes(c.key)).map((c) => c.key)])
  }
  return (
    <div className="folder-picker">
      <TextInput
        type="search"
        placeholder="Find a folder or machine"
        aria-label="Find a folder or machine"
        value={find}
        onChange={(e) => setFind(e.currentTarget.value)}
      />
      <div className="folder-scroll">
        <Chip.Group multiple value={selected} onChange={onChange}>
          {groups.map(([host, folders]) => (
            <section key={host || 'local'} className="folder-group">
              <h3 className={host ? 'folder-group-name is-host' : 'folder-group-name'}>{host || hostLabel}</h3>
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
        {visible.length === 0 && <p className="folder-empty">No folders match your search.</p>}
      </div>
      <div className="folder-foot">
        <Button disabled={selected.length === 0} onClick={() => onChange([])}>
          Clear
        </Button>
        <Button disabled={visible.length === 0} onClick={invert}>
          Invert
        </Button>
        <span className="folder-foot-status">
          {selected.length ? `${plural(selected.length, 'folder')} selected` : 'All folders shown'}
        </span>
        <Button variant="light" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  )
}

/** The folder filter: a bottom sheet on a phone, a popover on a wider screen.
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
          title="Folders"
          classNames={{ content: 'folder-sheet', body: 'folder-sheet-body' }}
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
