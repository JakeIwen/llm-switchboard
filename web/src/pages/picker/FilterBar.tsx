import { useState } from 'react'
import { Button, Menu, Tooltip } from '@mantine/core'
import { IconArrowsSort, IconCheck, IconChevronDown, IconFolder } from '@tabler/icons-react'
import { FolderPicker, folderTriggerLabel } from './FolderPicker'
import {
  SORT_LABELS,
  STATUS_LABELS,
  TOOL_LABELS,
  type FolderChoice,
  type SortKey,
  type StatusFilter,
  type ToolFilter,
} from './pickerModel'
import './FilterBar.css'

interface Props {
  status: StatusFilter
  tool: ToolFilter
  sort: SortKey
  folders: string[]
  folderChoices: FolderChoice[]
  hostLabel: string
  showTool: boolean
  onStatus: (v: StatusFilter) => void
  onTool: (v: ToolFilter) => void
  onSort: (v: SortKey) => void
  onFolders: (keys: string[]) => void
  onReset: () => void
}

const parts = { label: 'filter-button-inner', section: 'filter-button-section' }

function showLabel(status: StatusFilter, tool: ToolFilter): string {
  if (tool === 'all') return STATUS_LABELS[status]
  return status === 'all' ? `${TOOL_LABELS[tool]} only` : `${STATUS_LABELS[status]} · ${TOOL_LABELS[tool]}`
}

function MenuChoices<K extends string>({ title, labels, value, onChange }: {
  title: string
  labels: Record<K, string>
  value: K
  onChange: (value: K) => void
}) {
  return (
    <>
      <Menu.Label>{title}</Menu.Label>
      {(Object.keys(labels) as K[]).map((key) => (
        <Menu.Item key={key} onClick={() => onChange(key)} rightSection={key === value ? <IconCheck size={14} /> : null}>
          {labels[key]}
        </Menu.Item>
      ))}
    </>
  )
}

/** One row of fixed-width buttons that never move: each shows its current
    value and is tinted when it isn't the default; Reset is always there. */
export function FilterBar(p: Props) {
  const [foldersOpen, setFoldersOpen] = useState(false)
  const showActive = p.status !== 'all' || p.tool !== 'all'
  const anyActive = showActive || p.folders.length > 0
  const folderTrigger = (
    <Button
      className={`filter-button filter-folders${p.folders.length ? ' is-active' : ''}`}
      leftSection={<IconFolder size={14} />}
      rightSection={<IconChevronDown size={12} />}
      classNames={parts}
      onClick={() => setFoldersOpen((o) => !o)}
      aria-haspopup="dialog"
      aria-expanded={foldersOpen}
    >
      <span className="filter-button-label">{folderTriggerLabel(p.folders, p.folderChoices)}</span>
    </Button>
  )
  return (
    <div className="filter-bar">
      <Menu>
        <Menu.Target>
          <Button
            className={`filter-button filter-show${showActive ? ' is-active' : ''}`}
            rightSection={<IconChevronDown size={12} />}
            classNames={parts}
          >
            <span className="filter-button-label">{showLabel(p.status, p.tool)}</span>
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          <MenuChoices title="Show" labels={STATUS_LABELS} value={p.status} onChange={p.onStatus} />
          {p.showTool && (
            <>
              <Menu.Divider />
              <MenuChoices title="Tool" labels={TOOL_LABELS} value={p.tool} onChange={p.onTool} />
            </>
          )}
        </Menu.Dropdown>
      </Menu>
      <FolderPicker
        target={folderTrigger}
        opened={foldersOpen}
        onOpenChange={setFoldersOpen}
        choices={p.folderChoices}
        selected={p.folders}
        hostLabel={p.hostLabel}
        onChange={p.onFolders}
      />
      <Menu position="bottom-end">
        <Menu.Target>
          <Button
            className="filter-button filter-sort"
            aria-label={`Sort: ${SORT_LABELS[p.sort]}`}
            leftSection={<IconArrowsSort size={14} />}
            classNames={parts}
          >
            <span className="filter-button-label">{SORT_LABELS[p.sort]}</span>
          </Button>
        </Menu.Target>
        <Menu.Dropdown>
          <MenuChoices title="Sort" labels={SORT_LABELS} value={p.sort} onChange={p.onSort} />
        </Menu.Dropdown>
      </Menu>
      <Tooltip label="Show everything again: all sessions, tools and folders">
        <Button className="filter-button filter-reset" variant="subtle" disabled={!anyActive} onClick={p.onReset}>
          Reset
        </Button>
      </Tooltip>
    </div>
  )
}
