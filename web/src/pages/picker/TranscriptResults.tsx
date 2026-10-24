import { useEffect, useState } from 'react'
import { Button, Text } from '@mantine/core'
import { IconArrowLeft } from '@tabler/icons-react'
import { getJson } from '../../api/client'
import type { SessionRow as Row } from '../../api/sessionRow'
import { age, plural } from '../../format/format'
import { transcriptIndexStatus, type TranscriptIndexStatus } from '../../search/sessionQuery'
import { SessionRow } from './SessionRow'
import type { RowActions } from './SessionDetails'
import type { RowHandlers } from './SessionList'
import type { RowNotice } from './useHostActions'
import './TranscriptResults.css'

export interface TranscriptResult {
  tool: string
  id: string
  short_id: string
  title: string
  cwd: string
  project: string
  mtime: number
  count: number
  matched_at: number
  excerpt: string
  resume_cmd: string
  host: string
  title_only?: boolean
}

interface ContextLine {
  role: string
  text: string
  match: boolean
}

interface Props extends RowHandlers {
  /** The submitted query; editing the box doesn't search until Enter. */
  query: string
  ignored: readonly string[]
  rows: readonly Row[]
  expanded: ReadonlySet<string>
  selected: ReadonlySet<string>
  now: number
  actions: RowActions
  notices: ReadonlyMap<string, RowNotice>
  cursor: string | null
  /** Changes on every poll, to refresh the index state. */
  pollKey: number
  onBack: () => void
  onCopied: (ok: boolean) => void
}

type Results = { results: TranscriptResult[] } | { error: string } | null

function Context({ hit, query }: { hit: TranscriptResult; query: string }) {
  const [state, setState] = useState<{ contexts: { lines: ContextLine[] }[] } | { error: string } | null>(null)
  useEffect(() => {
    getJson<{ detail: { contexts: { lines: ContextLine[] }[] } }>(`/api/search/${hit.tool}/${hit.id}?q=${encodeURIComponent(query)}`)
      .then((d) => setState({ contexts: d.detail.contexts }))
      .catch((e: Error) => setState({ error: `Couldn't load matching context: ${e.message}` }))
  }, [hit, query])
  if (state === null) return <Text size="xs" c="dimmed">Loading matches…</Text>
  if ('error' in state) return <p className="transcript-error">{state.error}</p>
  if (state.contexts.length === 0) return <Text size="xs" c="dimmed">No context available.</Text>
  return (
    <div className="transcript-contexts">
      {state.contexts.map((c, i) => (
        <div key={i} className="transcript-context">
          {c.lines.map((line, j) => (
            <p key={j} className={line.match ? 'transcript-line is-match' : 'transcript-line'}>
              <span className="transcript-role">{line.role === 'user' ? 'You' : 'Reply'}</span>
              {line.text}
            </p>
          ))}
        </div>
      ))}
    </div>
  )
}

function Hit({ hit, now, query }: { hit: TranscriptResult; now: number; query: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="transcript-hit">
      <span className="transcript-hit-count">
        {hit.title_only
          ? 'Title or folder match only; the Mac keeps its transcripts'
          : `${plural(hit.count, 'match', 'matches')}, latest ${age(hit.matched_at, now)}`}
      </span>
      {!hit.title_only && <p className="transcript-hit-excerpt">{hit.excerpt}</p>}
      {!hit.title_only && (
        <Button variant="subtle" className="transcript-open" onClick={() => setOpen((o) => !o)}>
          {open ? 'Hide context' : 'Open matches'}
        </Button>
      )}
      {open && <Context hit={hit} query={query} />}
    </div>
  )
}

/** Transcripts mode: a scope line in place of the filters, and each matching
    session as an ordinary row with its match count and excerpt. */
export function TranscriptResults(p: Props) {
  const [results, setResults] = useState<Results>(null)
  const [index, setIndex] = useState<TranscriptIndexStatus | null>(null)
  useEffect(() => {
    if (!p.query) return setResults(null)
    const abort = new AbortController()
    setResults(null)
    getJson<{ results: TranscriptResult[] }>(`/api/search?q=${encodeURIComponent(p.query)}`, abort.signal)
      .then((d) => setResults({ results: d.results }))
      .catch((e: Error) => {
        if (!abort.signal.aborted) setResults({ error: `Transcript search failed: ${e.message}` })
      })
    return () => abort.abort()
  }, [p.query])
  useEffect(() => {
    getJson<{ index: TranscriptIndexStatus }>('/api/search/status')
      .then((d) => setIndex(d.index))
      .catch(() => {})
  }, [p.pollKey])
  const byId = new Map(p.rows.map((r) => [r.id, r]))
  const found = results && 'results' in results ? results.results : null
  const indexText = transcriptIndexStatus(index, p.now)

  return (
    <div className="transcript-results">
      <div className="transcript-scope">
        <Button variant="subtle" leftSection={<IconArrowLeft size={14} />} onClick={p.onBack}>
          Sessions
        </Button>
        <Text size="sm" className="transcript-scope-label">
          <strong>Transcripts</strong>
          {!p.query ? '' : found ? ` · ${plural(found.length, 'session')}` : results ? '' : ' · searching…'}
          <span className="transcript-index" title={indexText}>
            {' · '}
            {index?.state === 'ready' ? `index updated ${age(index.last_updated, p.now) || 'just now'}` : indexText}
          </span>
        </Text>
      </div>
      {p.ignored.length > 0 && <p className="transcript-ignored">Ignored inside transcripts: {p.ignored.join(' · ')}</p>}
      {!p.query && <p className="transcript-none">Type a transcript search and press Enter.</p>}
      {results && 'error' in results && <p className="transcript-error">{results.error}</p>}
      {found && found.length > 0 && (
        <div className="transcript-list">
          {found.map((hit) => {
            const row = byId.get(hit.id)
            return row ? (
              <SessionRow
                key={hit.id}
                row={row}
                expanded={p.expanded.has(row.id)}
                selected={p.selected.has(row.id)}
                now={p.now}
                onExpand={p.onExpand}
                onSelect={p.onSelect}
                onKeep={p.onKeep}
                onFolder={p.onFolder}
                actions={p.actions}
                notice={p.notices.get(row.id)}
                extra={<Hit hit={hit} now={p.now} query={p.query} />}
              />
            ) : null
          })}
        </div>
      )}
      {found && found.length === 0 && <p className="transcript-none">No transcripts match.</p>}
    </div>
  )
}
