import { useEffect, useState } from 'react'
import { getJson } from '../../api/client'
import type { HostStatus } from '../../api/sessionsPayload'
import { macHit, mergeTranscriptResults, type TranscriptHit } from '../../search/transcriptResults'
import { getMacSearchResults, macSearchUrls } from './macSearch'

type Results = { results: TranscriptHit[] } | { error: string } | null

/** Pi results remain usable while the browser tries its own Mac's loopback search. */
export function useTranscriptSearch(query: string, hosts: readonly HostStatus[]) {
  const [pi, setPi] = useState<Results>(null)
  const [mac, setMac] = useState<TranscriptHit[]>([])
  const [macFailed, setMacFailed] = useState(false)
  // Polls replace the host records; only endpoint changes should resubmit the query.
  const endpointKey = macSearchUrls(hosts, {
    secureContext: window.isSecureContext,
    protocol: window.location.protocol,
    userAgent: navigator.userAgent,
  }).join('\n')

  useEffect(() => {
    const abort = new AbortController()
    setPi(null)
    setMac([])
    setMacFailed(false)
    if (!query) return () => abort.abort()
    getJson<{ results: TranscriptHit[] }>(`/api/search?q=${encodeURIComponent(query)}`, abort.signal)
      .then((data) => { if (!abort.signal.aborted) setPi({ results: data.results }) })
      .catch((error: Error) => {
        if (!abort.signal.aborted) setPi({ error: `Transcript search failed: ${error.message}` })
      })
    if (endpointKey) {
      const requests = endpointKey.split('\n').map(async (searchUrl) => {
        const data = await getMacSearchResults(searchUrl, query, abort.signal)
        return data.results.map((hit) => macHit(hit, searchUrl))
      })
      void Promise.allSettled(requests).then((responses) => {
        if (abort.signal.aborted) return
        setMac(responses.flatMap((response) => response.status === 'fulfilled' ? response.value : []))
        setMacFailed(responses.some((response) => response.status === 'rejected'))
      })
    }
    return () => abort.abort()
  }, [query, endpointKey])

  const results = pi && 'results' in pi && mac.length
    ? { results: mergeTranscriptResults(pi.results, mac) }
    : pi
  return { results, macFailed }
}
