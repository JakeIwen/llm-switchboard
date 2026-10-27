import type { components } from '../api/schema'

export type TranscriptResult = components['schemas']['SearchResult']

/** Where a hit from a Mac's own endpoint came from: page-local, never sent to the Pi. */
export interface MacSource { searchUrl: string; sessionId: string }
export type TranscriptHit = TranscriptResult & { mac?: MacSource }

/** The Mac answers with bare session ids; the Pi's rows for its sessions are `<host>:<id>`
    (hosts.page_row), so re-key the hit to match its row and its title-only Pi result. */
export function macHit(hit: TranscriptResult, searchUrl: string): TranscriptHit {
  return { ...hit, id: `${hit.host}:${hit.id}`, mac: { searchUrl, sessionId: hit.id } }
}

const resultKey = (hit: TranscriptResult): string => `${hit.tool}:${hit.id}`
const matchedAt = (hit: TranscriptResult): number => hit.matched_at || hit.mtime || 0

/** Keep real transcript hits over metadata-only matches, without duplicating sessions. */
export function mergeTranscriptResults(
  pi: readonly TranscriptHit[],
  mac: readonly TranscriptHit[],
): TranscriptHit[] {
  const merged = new Map(pi.map((hit) => [resultKey(hit), hit]))
  for (const hit of mac) {
    const key = resultKey(hit)
    const existing = merged.get(key)
    if (!existing || (existing.title_only && !hit.title_only)) merged.set(key, hit)
  }
  return [...merged.values()].sort((a, b) => matchedAt(b) - matchedAt(a))
}
