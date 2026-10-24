import { useCallback, useMemo, useRef, useState } from 'react'
import { Container } from '@mantine/core'
import type { SessionRow } from '../api/sessionRow'
import { nowS } from '../format/format'
import { FilterBar } from './picker/FilterBar'
import { Header } from './picker/Header'
import { SearchBox, type SearchMode } from './picker/SearchBox'
import { SessionList } from './picker/SessionList'
import { TranscriptResults } from './picker/TranscriptResults'
import { useSessions } from './picker/useSessions'
import {
  dirKey,
  filterRows,
  folderChoices,
  sortRows,
  type SortKey,
  type StatusFilter,
  type ToolFilter,
} from './picker/pickerModel'
import './PickerPage.css'

const toggle = (set: ReadonlySet<string>, id: string) => {
  const next = new Set(set)
  if (!next.delete(id)) next.add(id)
  return next
}

export function PickerPage() {
  const { data, error } = useSessions()
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<SearchMode>('sessions')
  const [status, setStatus] = useState<StatusFilter>('all')
  const [tool, setTool] = useState<ToolFilter>('all')
  const [sort, setSort] = useState<SortKey>('kept')
  const [folders, setFolders] = useState<string[]>([])
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const search = useRef<HTMLInputElement>(null)

  const rows = useMemo(() => data?.sessions ?? [], [data])
  const matching = useMemo(() => filterRows(rows, query, status, tool), [rows, query, status, tool])
  const shown = useMemo(() => {
    const picked = new Set(folders)
    const inFolders = picked.size ? matching.filter((r) => picked.has(dirKey(r.host, r.project))) : matching
    return sortRows(inFolders, sort)
  }, [matching, folders, sort])
  const choices = useMemo(
    () => folderChoices(rows, matching, data?.settings.folders ?? { depth: 2, min_sessions: 2, hidden: [] }),
    [rows, matching, data],
  )

  const onExpand = useCallback((id: string) => setExpanded((s) => toggle(s, id)), [])
  const onSelect = useCallback((id: string) => setSelected((s) => toggle(s, id)), [])
  const onKeep = useCallback((_row: SessionRow, _keep: boolean) => {}, [])
  const onReset = useCallback(() => {
    setStatus('all')
    setTool('all')
    setFolders([])
  }, [])
  const onFolder = useCallback((row: SessionRow) => {
    const key = dirKey(row.host, row.project)
    setFolders((f) => (f.length === 1 && f[0] === key ? [] : [key]))
  }, [])

  const now = data?.now ?? nowS()
  return (
    <Container size={860} px="lg" className="picker">
      <Header data={data} error={error} onNew={() => {}} onSettings={() => {}} />
      <div className="picker-toolbar">
        <SearchBox
          ref={search}
          value={query}
          mode={mode}
          onChange={(v) => {
            setQuery(v)
            if (!v) setMode('sessions')
          }}
          onSubmit={() => query.trim() && setMode('transcripts')}
          onEscape={() => setMode('sessions')}
        />
        {mode === 'sessions' && (
          <FilterBar
            status={status}
            tool={tool}
            sort={sort}
            folders={folders}
            folderChoices={choices}
            hostLabel={data?.host ?? 'This Pi'}
            showTool={data?.codex.enabled ?? true}
            onStatus={setStatus}
            onTool={setTool}
            onSort={setSort}
            onFolders={setFolders}
            onReset={onReset}
          />
        )}
      </div>
      {mode === 'transcripts' ? (
        <TranscriptResults
          query={query.trim()}
          rows={rows}
          expanded={expanded}
          selected={selected}
          now={now}
          onExpand={onExpand}
          onSelect={onSelect}
          onKeep={onKeep}
          onFolder={onFolder}
          onBack={() => setMode('sessions')}
        />
      ) : (
        <SessionList
          rows={shown}
          total={rows.length}
          query={query}
          showSize={sort === 'size'}
          filtered={status !== 'all' || tool !== 'all' || folders.length > 0}
          expanded={expanded}
          selected={selected}
          now={now}
          onExpand={onExpand}
          onSelect={onSelect}
          onKeep={onKeep}
          onFolder={onFolder}
          onSearchTranscripts={() => setMode('transcripts')}
          onReset={onReset}
        />
      )}
    </Container>
  )
}
