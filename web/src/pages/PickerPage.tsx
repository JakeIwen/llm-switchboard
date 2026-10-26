import { useCallback, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Container } from '@mantine/core'
import { BulkBar } from './picker/BulkBar'
import { FilterBar } from './picker/FilterBar'
import { Header } from './picker/Header'
import { Notices } from './picker/Notices'
import { SearchBox } from './picker/SearchBox'
import { SessionList } from './picker/SessionList'
import { Toast } from './picker/Toast'
import { TranscriptResults } from './picker/TranscriptResults'
import { TrashSection } from './picker/TrashSection'
import { useActionUi } from './picker/ActionDialogs'
import { usePickerActions } from './picker/usePickerActions'
import { useHostActions } from './picker/useHostActions'
import { useKeyboard } from './picker/useKeyboard'
import { usePickerFilters } from './picker/usePickerFilters'
import { useSelection, type SelectEvent } from './picker/useSelection'
import { useSessions } from './picker/useSessions'
import './PickerPage.css'

// Room the selection bar takes at the bottom, so the toast sits above it.
const BULKBAR_SPACE = '64px'
// Rows show ages in whole minutes, so they get a clock that moves once a
// minute; with the poll's exact time every row would re-render every poll.
const minuteClock = (now: number) => Math.floor(now / 60) * 60

const toggle = (set: ReadonlySet<string>, id: string) => {
  const next = new Set(set)
  if (!next.delete(id)) next.add(id)
  return next
}

/** The one page: wiring only. State lives in the hooks beside it. */
export function PickerPage() {
  const sessions = useSessions()
  const { data, error } = sessions
  const filters = usePickerFilters(data)
  const { ui, dialogs, toast, dismissToast, progress, dialogOpen } = useActionUi()
  const order = useMemo(() => filters.shown.map((r) => r.id), [filters.shown])
  const selection = useSelection(order)
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())
  const search = useRef<HTMLInputElement>(null)
  const notices = useHostActions(data, ui)

  const reveal = useCallback(
    (id: string) => {
      setExpanded((s) => new Set(s).add(id))
      selection.moveCursor(id)
      requestAnimationFrame(() => document.querySelector(`[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest' }))
    },
    [selection],
  )
  const forget = useCallback(
    (ids: Iterable<string>) => {
      const gone = new Set(ids)
      selection.drop(gone)
      setExpanded((s) => new Set([...s].filter((id) => !gone.has(id))))
    },
    [selection],
  )
  const act = usePickerActions({ ui, sessions, filters, selection, reveal, forget })

  const onExpand = useCallback(
    (id: string, e: SelectEvent) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey) return selection.modifierClick(id, e)
      setExpanded((s) => toggle(s, id))
      selection.moveCursor(id)
    },
    [selection],
  )

  const sessionsMode = filters.mode === 'sessions'
  useKeyboard({
    active: Boolean(data) && sessionsMode && !dialogOpen,
    leaveTranscripts: !sessionsMode && !dialogOpen ? filters.backToSessions : null,
    move: (delta, extend) => {
      const id = selection.step(delta, extend)
      if (id) document.querySelector(`[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest' })
    },
    toggleSelect: selection.toggleCursor,
    toggleExpand: () => selection.cursor && setExpanded((s) => toggle(s, selection.cursor!)),
    selectAll: selection.selectAll,
    clear: selection.clear,
    remove: () => {
      const ids = selection.count ? selection.list() : selection.cursor ? [selection.cursor] : []
      if (ids.length) act.bulk('delete', ids)
    },
    focusSearch: () => search.current?.focus(),
    newSession: act.newSession,
  })

  const lines = [
    ...(sessionsMode ? filters.queryErrors : []),
    ...(data?.codex.error ? [`Codex sessions unavailable: ${data.codex.error}`] : []),
    ...(error && data ? [`Can't reach the service: ${error}; showing the last list`] : []),
  ]
  const showBar = sessionsMode && (selection.count > 0 || progress !== null)
  const style = { '--bulkbar-space': showBar ? BULKBAR_SPACE : '0px' } as CSSProperties
  const rowProps = {
    expanded,
    selected: selection.ids,
    now: minuteClock(filters.now),
    onExpand,
    onSelect: selection.onSelect,
    onKeep: act.keep,
    onFolder: filters.toggleFolder,
    actions: act.row,
    notices,
    cursor: selection.cursor,
  }

  return (
    <Container size={860} px="lg" className={showBar ? 'picker has-bulkbar' : 'picker'} style={style}>
      <Header data={data} error={error} onNew={act.newSession} onSettings={act.settings} />
      <div className="picker-toolbar">
        <SearchBox
          ref={search}
          value={filters.query}
          mode={filters.mode}
          onChange={filters.setQuery}
          onSubmit={filters.submit}
          onEscape={() => (sessionsMode ? search.current?.blur() : filters.backToSessions())}
        />
        {sessionsMode && (
          <FilterBar
            status={filters.status}
            tool={filters.tool}
            sort={filters.sort}
            folders={filters.folders}
            folderChoices={filters.choices}
            folderMachines={filters.machines}
            hostLabel={data?.host ?? 'This Pi'}
            showTool={filters.toolApplies}
            headless={filters.headless}
            showHeadless={filters.hasHeadless}
            onStatus={filters.setStatus}
            onTool={filters.setTool}
            onHeadless={filters.setHeadless}
            onSort={filters.setSort}
            onFolders={filters.setFolders}
            onReset={filters.reset}
          />
        )}
      </div>
      <Notices lines={lines} />
      {!data && <p className="picker-loading">{error ? `Can't reach the service: ${error}` : 'loading…'}</p>}
      {data && !sessionsMode && (
        <TranscriptResults
          {...rowProps}
          query={filters.submitted}
          ignored={filters.ignored}
          rows={filters.rows}
          pollKey={data.now}
          onBack={filters.backToSessions}
          onCopied={act.copied}
        />
      )}
      {data && sessionsMode && (
        <>
          <SessionList
            {...rowProps}
            rows={filters.shown}
            total={filters.rows.length}
            query={filters.query}
            showSize={filters.sort === 'size'}
            filtered={filters.filtered}
            onSearchTranscripts={filters.submit}
            onReset={filters.reset}
          />
          <TrashSection items={sessions.trash} now={filters.now} onRestore={act.restoreTrash} onEmpty={act.emptyTrash} />
        </>
      )}
      {showBar && <BulkBar count={selection.count} progress={progress} onAction={(a) => act.bulk(a, selection.list())} />}
      <Toast toast={toast} onDismiss={dismissToast} />
      {dialogs}
    </Container>
  )
}
