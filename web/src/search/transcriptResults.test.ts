import assert from 'node:assert/strict'
import { test } from 'vitest'
import { macHit, mergeTranscriptResults, type TranscriptHit } from './transcriptResults'

const hit = (overrides: Partial<TranscriptHit>): TranscriptHit => ({
  tool: 'claude', id: 'session', short_id: 'session', title: 'Session', cwd: '/work', project: '/work',
  mtime: 100, count: 1, matched_at: 90, excerpt: 'match', resume_cmd: '', host: '', title_only: false,
  ...overrides,
})

test('Mac transcript hits replace Pi title-only matches and retain their context endpoint', () => {
  // The Pi's title-only result carries its page row id; the Mac answers with the bare id.
  const title = hit({ id: 'mac:session', title_only: true, host: 'mac' })
  const full = macHit(hit({ host: 'mac', excerpt: 'Mac transcript' }), 'http://127.0.0.1:8778')
  assert.equal(full.id, 'mac:session')
  assert.deepEqual(full.mac, { searchUrl: 'http://127.0.0.1:8778', sessionId: 'session' })
  assert.deepEqual(mergeTranscriptResults([title], [full]), [full])
  assert.equal(mergeTranscriptResults([title], [full])[0], full)
  assert.equal(title.title_only, true)
})

test('merge adds Mac-only hits and keeps Pi full-text hits over duplicate Mac results', () => {
  const pi = hit({ excerpt: 'Pi transcript' })
  const duplicate = hit({ host: 'mac' })
  const macOnly = hit({ id: 'mac-only', host: 'mac', matched_at: 200 })
  assert.deepEqual(mergeTranscriptResults([pi], [duplicate, macOnly]), [macOnly, pi])
})

test('session identity includes both tool and id; title-only Mac hits cannot replace full text', () => {
  const pi = hit({ tool: 'claude' })
  const macCodex = hit({ tool: 'codex', host: 'mac' })
  const macTitle = hit({ title_only: true, host: 'mac' })
  assert.deepEqual(mergeTranscriptResults([pi], [macCodex, macTitle]), [pi, macCodex])
})

test('merged hits sort by matched_at, then mtime fallback, newest first without mutating inputs', () => {
  const pi = [hit({ id: 'old', matched_at: 10, mtime: 1000 }), hit({ id: 'unknown', matched_at: null, mtime: null })]
  const mac = [hit({ id: 'fallback', matched_at: null, mtime: 500 }), hit({ id: 'zero', matched_at: 0, mtime: 100 })]
  assert.deepEqual(mergeTranscriptResults(pi, mac).map((row) => row.id), ['fallback', 'zero', 'old', 'unknown'])
  assert.deepEqual(pi.map((row) => row.id), ['old', 'unknown'])
  assert.deepEqual(mac.map((row) => row.id), ['fallback', 'zero'])
})

test('duplicate Mac hits are added only once and a later full hit upgrades a title-only hit', () => {
  const title = hit({ host: 'mac', title_only: true })
  const full = hit({ host: 'mac' })
  assert.deepEqual(mergeTranscriptResults([], [title, full, full]), [full])
  assert.deepEqual(mergeTranscriptResults([], []), [])
})
