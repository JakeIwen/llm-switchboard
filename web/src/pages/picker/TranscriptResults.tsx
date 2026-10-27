import { useEffect, useState } from 'react'
import { Button, Text } from '@mantine/core'
import { IconArrowLeft } from '@tabler/icons-react'
import { getJson } from '../../api/client'
import type { SessionRow as Row } from '../../api/sessionRow'
import type { HostStatus } from '../../api/sessionsPayload'
import type { TranscriptHit } from '../../search/transcriptResults'
import { getMacMatchContexts, type MatchContexts } from './macSearch'
import { useTranscriptSearch } from './useTranscriptSearch'
import { age, plural } from '../../format/format'
import { transcriptIndexStatus, type TranscriptIndexStatus } from '../../search/sessionQuery'
import { SessionRow } from './SessionRow'
import type { RowActions } from './SessionDetails'
import type { RowHandlers } from './SessionList'
import type { RowNotice } from './useHostActions'
import './TranscriptResults.css'

interface Props extends RowHandlers {
  /** The submitted query; editing the box doesn't search until Enter. */
  query: string
  ignored: readonly string[]
  rows: readonly Row[]
  hosts: readonly HostStatus[]
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

function Context({ hit, query }: { hit: TranscriptHit; query: string }) {
  const [state, setState] = useState<MatchContexts | { error: string } | null>(null)
  useEffect(() => {
    const abort = new AbortController()
    setState(null)
    const request = hit.mac
      ? getMacMatchContexts(hit.mac.searchUrl, hit.tool, hit.mac.sessionId, query, abort.signal)
      : getJson<{ detail: MatchContexts }>(`/api/search/${hit.tool}/${hit.id}?q=${encodeURIComponent(query)}`, abort.signal)
    request.then((data) => { if (!abort.signal.aborted) setState({ contexts: data.detail.contexts }) })
      .catch((error: Error) => {
        if (!abort.signal.aborted) setState({ error: `Couldn't load matching context: ${error.message}` })
      })
    return () => abort.abort()
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

function Hit({ hit, now, query }: { hit: TranscriptHit; now: number; query: string }) {
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
  const { results, macFailed } = useTranscriptSearch(p.query, p.hosts)
  const [index, setIndex] = useState<TranscriptIndexStatus | null>(null)
  useEffect(() => {
    getJson<{ index: TranscriptIndexStatus }>('/api/search/status')
      .then((d) => setIndex(d.index))
      .catch(() => {})
  }, [p.pollKey])
  const byId = new Map(p.rows.map((r) => [r.id, r]))
  const found = results && 'results' in results ? results.results : null
  const indexText = transcriptIndexStatus(index, p.now)
  const hidden = found ? found.filter((hit) => !byId.has(hit.id)).length : 0

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
          {macFailed && (
            <span title="The Mac search endpoint didn't answer. Mac sessions are matched by title or folder only.">
              {' · Mac: titles only'}
            </span>
          )}
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
      {found && found.length > 0 && hidden > 0 && (
        <p className="transcript-ignored">
          {plural(hidden, 'more session')} matched but {hidden === 1 ? "isn't" : "aren't"} in the list (deleted, or not loaded yet).
        </p>
      )}
      {found && found.length === 0 && <p className="transcript-none">No transcripts match.</p>}
    </div>
  )
}
