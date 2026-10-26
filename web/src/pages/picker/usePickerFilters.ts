import { useCallback, useMemo, useState } from 'react'
import type { SessionRow } from '../../api/sessionRow'
import type { SessionsPayload } from '../../api/sessionsPayload'
import { dirKey, folderChoices } from '../../folders/folderChoices'
import { readFolders, writeFolders } from '../../folders/folderSelection'
import { transcriptQuery } from '../../search/sessionQuery'
import { sortRows, type SortKey } from '../../sessions/sort'
import {
  inFolders,
  matchingRows,
  saveHeadless,
  saveSort,
  saveTool,
  savedHeadless,
  savedSort,
  savedTool,
  toolFilterApplies,
  type StatusFilter,
  type ToolFilter,
} from './pickerModel'

function storage(): Storage | undefined {
  try {
    return window.localStorage
  } catch {
    return undefined
  }
}

export type SearchMode = 'sessions' | 'transcripts'

/** Everything the toolbar controls, the rows it leaves, and the search mode.
    Entering Transcripts keeps the Sessions query to restore on the way back. */
export function usePickerFilters(data: SessionsPayload | null) {
  const [query, setQueryText] = useState('')
  const [mode, setMode] = useState<SearchMode>('sessions')
  const [sessionsQuery, setSessionsQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [ignored, setIgnored] = useState<string[]>([])
  const [status, setStatus] = useState<StatusFilter>('all')
  const [tool, setToolState] = useState<ToolFilter>(savedTool)
  const [headless, setHeadlessState] = useState<boolean>(savedHeadless)
  const [sort, setSortState] = useState<SortKey>(savedSort)
  const [folders, setFoldersState] = useState<string[]>(() => [...readFolders(storage())])

  const rows = useMemo(() => data?.sessions ?? [], [data])
  const now = data?.now ?? Date.now() / 1000
  const toolApplies = toolFilterApplies(data?.codex.enabled ?? false, rows)
  const effectiveTool = toolApplies ? tool : 'all'
  const sessionText = mode === 'sessions' ? query : sessionsQuery

  const matching = useMemo(
    () => matchingRows(rows, sessionText, status, effectiveTool, headless, now),
    // `now` moves every poll with the rows; age: terms follow it.
    [rows, sessionText, status, effectiveTool, headless, now],
  )
  const picked = useMemo(() => new Set(folders), [folders])
  const shown = useMemo(() => sortRows(inFolders(matching.rows, picked), sort), [matching, picked, sort])
  const choices = useMemo(
    () =>
      folderChoices(rows, folders, {
        depth: data?.settings.folders.depth,
        minSessions: data?.settings.folders.min_sessions,
        hidden: data?.settings.folders.hidden,
      }, matching.rows),
    [rows, folders, data, matching],
  )

  const setFolders = useCallback((keys: string[]) => {
    setFoldersState(keys)
    writeFolders(storage(), keys)
  }, [])
  const setTool = useCallback((value: ToolFilter) => {
    setToolState(value)
    saveTool(value)
  }, [])
  const setHeadless = useCallback((value: boolean) => {
    setHeadlessState(value)
    saveHeadless(value)
  }, [])
  const setSort = useCallback((value: SortKey) => {
    setSortState(value)
    saveSort(value)
  }, [])

  const backToSessions = useCallback(() => {
    if (mode === 'sessions') return
    setMode('sessions')
    setQueryText(sessionsQuery)
    setSubmitted('')
    setIgnored([])
  }, [mode, sessionsQuery])

  /** Enter: from Sessions, carry the compatible terms into Transcripts; in
      Transcripts, run the edited query. */
  const submit = useCallback(() => {
    if (mode === 'sessions') {
      if (!query.trim()) return
      const bridged = transcriptQuery(query)
      setSessionsQuery(query)
      setQueryText(bridged.query)
      setIgnored(bridged.ignored)
      setSubmitted(bridged.query.trim())
      setMode('transcripts')
    } else {
      setSubmitted(query.trim())
    }
  }, [mode, query])

  const setQuery = useCallback(
    (text: string) => {
      setQueryText(text)
      if (!text && mode === 'transcripts') backToSessions()
    },
    [mode, backToSessions],
  )

  const toggleFolder = useCallback(
    (row: SessionRow) => {
      const key = dirKey(row.host, row.project)
      setFolders(folders.length === 1 && folders[0] === key ? [] : [key])
    },
    [folders, setFolders],
  )

  const reset = useCallback(() => {
    setStatus('all')
    setTool('all')
    setHeadless(false)
    setFolders([])
  }, [setTool, setHeadless, setFolders])

  return {
    rows,
    now,
    query,
    setQuery,
    mode,
    submit,
    backToSessions,
    submitted,
    ignored,
    status,
    setStatus,
    tool: effectiveTool,
    setTool,
    toolApplies,
    headless,
    setHeadless,
    hasHeadless: rows.some((row) => row.headless),
    sort,
    setSort,
    folders,
    setFolders,
    toggleFolder,
    reset,
    filtered: status !== 'all' || effectiveTool !== 'all' || folders.length > 0,
    queryErrors: matching.errors,
    shown,
    choices: choices.choices,
    machines: choices.machines,
  }
}
