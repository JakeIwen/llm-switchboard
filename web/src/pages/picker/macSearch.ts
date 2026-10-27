import type { HostStatus } from '../../api/sessionsPayload'
import type { TranscriptResult } from '../../search/transcriptResults'
import type { components } from '../../api/schema'

const MAC_SEARCH_TIMEOUT_MS = 3000

type LoopbackRequestInit = RequestInit & { targetAddressSpace: 'loopback' }
type SearchResults = { results: TranscriptResult[] }
export type MatchContexts = Pick<components['schemas']['SearchDetail'], 'contexts'>
type ContextResponse = { detail: MatchContexts }
interface BrowserEnvironment {
  secureContext: boolean
  protocol: string
  userAgent: string
}

export function macSearchUrls(hosts: readonly HostStatus[], browser: BrowserEnvironment): string[] {
  if (!browser.secureContext || browser.protocol !== 'https:' || !browser.userAgent.includes('Macintosh')) return []
  return [...new Set(hosts.map((host) => host.search_url).filter(Boolean))]
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function timestamp(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isFinite(value))
}

function result(value: unknown): value is TranscriptResult {
  if (!record(value)) return false
  return ['tool', 'id', 'short_id', 'title', 'cwd', 'project', 'excerpt', 'resume_cmd', 'host']
    .every((key) => typeof value[key] === 'string')
    && typeof value.count === 'number' && Number.isFinite(value.count)
    && typeof value.title_only === 'boolean'
    && timestamp(value.mtime) && timestamp(value.matched_at)
}

function searchResults(value: unknown): value is SearchResults {
  return record(value) && Array.isArray(value.results) && value.results.every(result)
}

function contextResponse(value: unknown): value is ContextResponse {
  if (!record(value) || !record(value.detail) || !Array.isArray(value.detail.contexts)) return false
  return value.detail.contexts.every((context: unknown) => record(context)
    && timestamp(context.matched_at) && Array.isArray(context.lines)
    && context.lines.every((line: unknown) => record(line)
      && typeof line.role === 'string' && typeof line.text === 'string'
      && typeof line.match === 'boolean' && timestamp(line.at)))
}

async function loopbackJson<T>(
  searchUrl: string,
  path: string,
  signal: AbortSignal,
  valid: (value: unknown) => value is T,
): Promise<T> {
  const abort = new AbortController()
  const onAbort = () => abort.abort()
  signal.addEventListener('abort', onAbort, { once: true })
  if (signal.aborted) abort.abort()
  const timer = setTimeout(onAbort, MAC_SEARCH_TIMEOUT_MS)
  try {
    // Chrome needs the explicit address space for its loopback permission and mixed-content exception.
    const init: LoopbackRequestInit = { targetAddressSpace: 'loopback', credentials: 'omit', signal: abort.signal }
    const response = await fetch(`${searchUrl.replace(/\/$/, '')}${path}`, init)
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
    const body: unknown = await response.json()
    if (!valid(body)) throw new Error('Invalid Mac search response')
    return body
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', onAbort)
  }
}

export function getMacSearchResults(searchUrl: string, query: string, signal: AbortSignal): Promise<SearchResults> {
  return loopbackJson(searchUrl, `/api/search?q=${encodeURIComponent(query)}`, signal, searchResults)
}

export function getMacMatchContexts(
  searchUrl: string, tool: string, id: string, query: string, signal: AbortSignal,
): Promise<ContextResponse> {
  const path = `/api/search/${encodeURIComponent(tool)}/${encodeURIComponent(id)}?q=${encodeURIComponent(query)}`
  return loopbackJson(searchUrl, path, signal, contextResponse)
}
