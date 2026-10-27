import assert from 'node:assert/strict'
import { afterEach, test, vi } from 'vitest'
import { getMacMatchContexts, getMacSearchResults, macSearchUrls } from './macSearch'
import type { HostStatus } from '../../api/sessionsPayload'
import type { TranscriptResult } from '../../search/transcriptResults'

const searchUrl = 'http://127.0.0.1:8778'
const browser = { secureContext: true, protocol: 'https:', userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X)' }
const hosts: HostStatus[] = [{ host: 'mac', last_seen: 100, online: true, search_url: searchUrl }]
const hit: TranscriptResult = {
  tool: 'claude', id: 'session', short_id: 'session', title: 'Session', cwd: '/work', project: '/work',
  mtime: 100, count: 1, matched_at: 90, excerpt: 'match', resume_cmd: '', host: 'mac', title_only: false,
}

const response = (body: unknown) => new Response(JSON.stringify(body), { status: 200 })

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

test('Mac search requires secure HTTPS, Macintosh user agent, and an advertised URL', () => {
  assert.deepEqual(macSearchUrls(hosts, browser), [searchUrl])
  assert.deepEqual(macSearchUrls(hosts, { ...browser, secureContext: false }), [])
  assert.deepEqual(macSearchUrls(hosts, { ...browser, protocol: 'http:' }), [])
  assert.deepEqual(macSearchUrls(hosts, { ...browser, userAgent: 'iPhone' }), [])
  assert.deepEqual(macSearchUrls(hosts, { ...browser, userAgent: 'Linux' }), [])
  assert.deepEqual(macSearchUrls([{ ...hosts[0], search_url: '' }], browser), [])
  assert.deepEqual(macSearchUrls([], browser), [])
  assert.deepEqual(macSearchUrls([...hosts, ...hosts], browser), [searchUrl])
})

test('Mac search is a plain credential-free loopback GET and returns validated results', async () => {
  const fetch = vi.fn().mockResolvedValue(response({ results: [hit] }))
  vi.stubGlobal('fetch', fetch)
  assert.deepEqual(await getMacSearchResults(`${searchUrl}/`, 'one & two', new AbortController().signal), { results: [hit] })
  assert.equal(fetch.mock.calls[0][0], `${searchUrl}/api/search?q=one%20%26%20two`)
  const init = fetch.mock.calls[0][1]
  assert.equal(init.credentials, 'omit')
  assert.equal(init.targetAddressSpace, 'loopback')
  assert.equal(init.headers, undefined)
  assert.equal(init.method, undefined)
  assert.equal(init.signal.aborted, false)
})

test('Open matches uses the originating endpoint and validates context lines', async () => {
  const detail = { contexts: [{ matched_at: 90, lines: [{ role: 'user', text: 'match', match: true, at: null }] }] }
  const fetch = vi.fn().mockResolvedValue(response({ detail }))
  vi.stubGlobal('fetch', fetch)
  assert.deepEqual(await getMacMatchContexts(searchUrl, 'codex', 'session', 'one & two', new AbortController().signal), { detail })
  assert.equal(fetch.mock.calls[0][0], `${searchUrl}/api/search/codex/session?q=one%20%26%20two`)
  assert.equal(fetch.mock.calls[0][1].targetAddressSpace, 'loopback')
  assert.equal(fetch.mock.calls[0][1].credentials, 'omit')
})

test('HTTP errors, invalid JSON and malformed search payloads reject rather than becoming hits', async () => {
  for (const reply of [
    new Response('refused', { status: 403 }),
    new Response('not JSON', { status: 200 }),
    response(null), response({}), response({ results: [null] }),
    response({ results: [{ ...hit, count: 'many' }] }),
  ]) {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply))
    await assert.rejects(getMacSearchResults(searchUrl, 'match', new AbortController().signal))
  }
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
  await assert.rejects(getMacSearchResults(searchUrl, 'match', new AbortController().signal), /Failed to fetch/)
})

test('malformed context responses reject before rendering', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ detail: { contexts: [{ lines: [{ text: 5 }] }] } })))
  await assert.rejects(getMacMatchContexts(searchUrl, 'claude', 'session', 'match', new AbortController().signal), /Invalid Mac search response/)
})

function pendingFetch() {
  const fetch = vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
    const abort = () => reject(new DOMException('Aborted', 'AbortError'))
    if (init.signal?.aborted) abort()
    else init.signal?.addEventListener('abort', abort, { once: true })
  }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}

test('Mac search times out after three seconds and clears its timer', async () => {
  vi.useFakeTimers()
  const fetch = pendingFetch()
  const request = getMacSearchResults(searchUrl, 'match', new AbortController().signal)
  const rejected = assert.rejects(request, { name: 'AbortError' })
  await vi.advanceTimersByTimeAsync(3000)
  await rejected
  assert.equal(fetch.mock.calls[0][1].signal?.aborted, true)
  assert.equal(vi.getTimerCount(), 0)
})

test('query cancellation aborts both search and context requests, including an already-aborted query', async () => {
  vi.useFakeTimers()
  const fetch = pendingFetch()
  const query = new AbortController()
  const search = getMacSearchResults(searchUrl, 'match', query.signal)
  const context = getMacMatchContexts(searchUrl, 'claude', 'session', 'match', query.signal)
  const rejections = [assert.rejects(search, { name: 'AbortError' }), assert.rejects(context, { name: 'AbortError' })]
  query.abort()
  await Promise.all(rejections)
  await assert.rejects(getMacSearchResults(searchUrl, 'other', query.signal), { name: 'AbortError' })
  assert.ok(fetch.mock.calls.every(([, init]) => init.signal?.aborted))
  assert.equal(vi.getTimerCount(), 0)
})
