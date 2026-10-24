import { TOOLS, type SessionRow } from '../api/sessionRow'
import { age } from '../format/format'

const TOKEN = /(-)?(?:([a-z]+):)?(?:"([^"]*)"?|(\S+))/gi
const UNITS = { m: 60, h: 3600, d: 86400, w: 604800 } as const
const AGE = /^(<=?|>=?)?(\d+(?:\.\d+)?)([mhdw])$/
const lower = (value: unknown): string => String(value ?? '').toLowerCase()
const has = (fields: unknown[], value: string): boolean => fields.some((field) => lower(field).includes(value))
type QueryRow = Pick<SessionRow, 'title' | 'first_prompt' | 'project' | 'cwd' | 'id' | 'rc_name' | 'enabled' | 'status' | 'open_elsewhere' | 'mtime' | 'tool' | 'codex_state'>
type Predicate = (session: QueryRow, now: number) => boolean
interface QueryError { error: string }
const IS = {
  kept: (s) => s.enabled,
  on: (s) => s.enabled,
  off: (s) => !s.enabled,
  running: (s) => s.enabled && s.status?.state === 'running',
  open: (s) => s.enabled || s.open_elsewhere.length > 0,
  elsewhere: (s) => s.open_elsewhere.length > 0,
  busy: (s) => Boolean(s.status?.busy) || s.open_elsewhere.some((p) => p.status === 'busy') || s.codex_state === 'active',
  attention: (s) => s.status?.state === 'attention',
  claude: (s) => s.tool !== TOOLS.codex,
  codex: (s) => s.tool === TOOLS.codex,
} satisfies Record<string, (session: QueryRow) => boolean>
export type IsValue = keyof typeof IS
export const IS_VALUES = Object.keys(IS) as IsValue[]

function agePredicate(value: string): Predicate | null {
  const match = AGE.exec(value)
  if (!match) return null
  const [, op = '<', n, unit] = match
  const limit = Number(n) * UNITS[unit as keyof typeof UNITS]
  return (session, now) => {
    if (!session.mtime) return false
    const elapsed = now - session.mtime
    return op.startsWith('<') ? (op === '<=' ? elapsed <= limit : elapsed < limit)
      : (op === '>=' ? elapsed >= limit : elapsed > limit)
  }
}
const folderPredicate = (value: string): Predicate => (s) => has([s.project, s.cwd], value)
const FIELD_PREDICATES = {
  dir: folderPredicate,
  folder: folderPredicate,
  in: folderPredicate,
  title: (value) => (s) => has([s.title, s.rc_name], value),
  id: (value) => (s) => lower(s.id).includes(value),
  is: (value) => IS[value as IsValue] ? (s) => IS[value as IsValue](s) : { error: `is: takes ${IS_VALUES.join(', ')}` },
  state: (value) => (s) => s.status?.state === value,
  age: (value) => agePredicate(value) || { error: 'age: looks like <2d, >1w, <30m' },
} satisfies Record<string, (value: string) => Predicate | QueryError>
const textPredicate = (value: string): Predicate => (s) => has([s.title, s.first_prompt, s.project, s.cwd, s.id, s.rc_name], value)

/** Carry only compatible terms into Transcripts, preserving raw phrases and exclusions. */
export function transcriptQuery(text: unknown): { query: string; ignored: string[] } {
  const terms: string[] = [], ignored: string[] = []
  for (const [raw, neg, rawField, quoted, bare] of String(text).matchAll(TOKEN)) {
    const field = lower(rawField), value = lower(quoted ?? bare)
    if (field === 'is' && (value === TOOLS.claude || value === TOOLS.codex)) {
      terms.push(`${neg || ''}tool:${value}`)
    } else if (['is', 'state', 'title', 'id'].includes(field) || /^-?(is|state|title|id):$/i.test(raw)) {
      ignored.push(raw)
    } else {
      terms.push(raw)
    }
  }
  return { query: terms.join(' '), ignored }
}

export interface CompiledQuery { test: Predicate; errors: string[]; empty: boolean }
export function compileQuery(text: unknown): CompiledQuery {
  const predicates: Predicate[] = [], errors: string[] = []
  for (const [, neg, rawField, quoted, bare] of String(text).matchAll(TOKEN)) {
    const value = lower(quoted ?? bare ?? '')
    const field = rawField && lower(rawField)
    if (!value || (!field && bare === '-' && !neg)) continue
    const predicate = field && Object.hasOwn(FIELD_PREDICATES, field)
      ? FIELD_PREDICATES[field as keyof typeof FIELD_PREDICATES](value)
      : textPredicate(field ? `${field}:${value}` : value)
    if ('error' in predicate) {
      errors.push(predicate.error)
      continue
    }
    predicates.push(neg ? (session, now) => !predicate(session, now) : predicate)
  }
  return { test: (session, now) => predicates.every((predicate) => predicate(session, now)), errors, empty: predicates.length === 0 }
}

export interface TranscriptIndexStatus {
  state: 'missing' | 'building' | 'ready' | 'error'
  sessions: number
  last_updated: number | null
  error?: string
}
/** Pure status text from fulltext.js; no DOM or polling is owned here. */
export function transcriptIndexStatus(status: TranscriptIndexStatus | null | undefined, now?: number): string {
  if (!status) return 'Transcript index: checking…'
  const count = status.sessions === 1 ? '1 session' : `${status.sessions} sessions`
  const text: Record<TranscriptIndexStatus['state'], () => string> = {
    building: () => `Transcript index: updating · ${count}`,
    ready: () => `Transcript index: ${count} · updated ${age(status.last_updated, now) || 'just now'}`,
    error: () => `Transcript index error: ${status.error || 'unknown error'}`,
    missing: () => 'Transcript index: not built yet',
  }
  return Object.hasOwn(text, status.state) ? text[status.state]() : text.missing()
}
