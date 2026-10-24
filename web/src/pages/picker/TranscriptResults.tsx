import { useEffect, useState } from 'react'
import { Button, Text } from '@mantine/core'
import { IconArrowLeft } from '@tabler/icons-react'
import { getJson } from '../../api/client'
import type { SessionRow as Row } from '../../api/sessionRow'
import { age, plural } from '../../format/format'
import { SessionRow } from './SessionRow'
import type { RowHandlers } from './SessionList'
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

interface Props extends RowHandlers {
  query: string
  rows: Row[]
  expanded: ReadonlySet<string>
  selected: ReadonlySet<string>
  now: number
  onBack: () => void
}

function Hit({ hit, now }: { hit: TranscriptResult; now: number }) {
  return (
    <div className="transcript-hit">
      <span className="transcript-hit-count">
        {hit.title_only ? 'Title or folder match (the Mac keeps its transcripts)' : `${plural(hit.count, 'match')}, latest ${age(hit.matched_at, now)}`}
      </span>
      {!hit.title_only && <p className="transcript-hit-excerpt">{hit.excerpt}</p>}
    </div>
  )
}

/** Transcripts mode: a scope line in place of the filters, and each matching
    session as an ordinary row with its match count and excerpt. */
export function TranscriptResults(p: Props) {
  const [state, setState] = useState<{ results: TranscriptResult[] } | { error: string } | null>(null)
  useEffect(() => {
    const abort = new AbortController()
    setState(null)
    getJson<{ results: TranscriptResult[] }>(`/api/search?q=${encodeURIComponent(p.query)}`, abort.signal)
      .then((d) => setState({ results: d.results }))
      .catch((e: Error) => {
        if (!abort.signal.aborted) setState({ error: e.message })
      })
    return () => abort.abort()
  }, [p.query])
  const byId = new Map(p.rows.map((r) => [r.id, r]))

  return (
    <div className="transcript-results">
      <div className="transcript-scope">
        <Button variant="subtle" leftSection={<IconArrowLeft size={14} />} onClick={p.onBack}>
          Sessions
        </Button>
        <Text size="sm" className="transcript-scope-label">
          <strong>Transcripts</strong>
          {state === null ? ' · searching…' : 'results' in state ? ` · ${plural(state.results.length, 'session')}` : ''}
        </Text>
      </div>
      {state && 'error' in state && <p className="transcript-error">{state.error}</p>}
      {state && 'results' in state && state.results.length > 0 && (
        <div className="transcript-list">
          {state.results.map((hit) => {
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
                extra={<Hit hit={hit} now={p.now} />}
              />
            ) : null
          })}
        </div>
      )}
      {state && 'results' in state && state.results.length === 0 && (
        <p className="transcript-none">No prompt or reply contains “{p.query}”.</p>
      )}
    </div>
  )
}
