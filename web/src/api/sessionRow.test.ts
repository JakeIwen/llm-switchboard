import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { expect, it } from 'vitest'
import type { SessionRow, SessionStatus } from './sessionRow'

// Exercise the actual public Python projections, not a parallel mock of the API.
function rowsFromPython(body: string): unknown {
  return JSON.parse(execFileSync('python3', ['-c', `
import json
from llm_switchboard.catalog import session_rows, codex_rows
from llm_switchboard.store import Entry
from llm_switchboard.supervisor_state import Status, Exit, State
from llm_switchboard.live_registry import LiveProcess
from llm_switchboard.codex_threads import CodexThread
from llm_switchboard.hosts import page_row
sid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
${body}
`], { cwd: fileURLToPath(new URL('../../../', import.meta.url)), encoding: 'utf8' }))
}
const id = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
const base = {
  id, tool: 'claude', host: '', short_id: 'aaaaaaaa', title: 'New', first_prompt: '',
  cwd: '/work/project', project: '/work/project', mtime: 10, size: null,
  remote_url: '', resume_cmd: `cd -- /work/project && claude --resume ${id}`,
  new: true, enabled: true, rc_name: '', open_elsewhere: [], status: null,
} satisfies SessionRow

it('types a pending local Claude row, including its nested supervisor and exit records', () => {
  const status: SessionStatus = {
    session_id: id, state: 'running', detail: '', window_id: null, pid: null, rss_kb: null,
    rc_active: false, busy: false, duplicate: false, started_at: null, next_attempt: 0,
    failures: 0, launches: 0, last_exit: { status: null, at: 12, ran_s: 3 }, rc_name: '',
  }
  const expected: SessionRow = { ...base, status, open_elsewhere: [{ pid: 42, status: 'idle' }] }
  expect(rowsFromPython(`
entry = Entry(sid, True, updated_at=10, cwd='/work/project', title='New')
status = Status(sid, State.RUNNING, last_exit=Exit(None, 12, 3, 'screen omitted'))
live = [LiveProcess(42, sid, '/work/project', 'idle', None, None)]
print(json.dumps(session_rows([], {sid: entry}, live, {sid: status})))`)).toEqual([expected])
})

it('types local Codex rows with no supervisor and Codex-specific state', () => {
  const expected: SessionRow = {
    ...base, tool: 'codex', short_id: 'eeeeeeee', title: 'Codex', first_prompt: 'Hello',
    mtime: null, size: 42, new: false, enabled: false, codex_state: 'active',
    resume_cmd: `cd -- /work/project && codex resume ${id}`,
  }
  expect(rowsFromPython(`
thread = CodexThread(sid, '/work/project', 'Codex', 'Hello', None, 42, 'active')
print(json.dumps(codex_rows([thread])))`)).toEqual([expected])
})

it('types host metadata without prompts or local process pids', () => {
  const { first_prompt: omitted, ...common } = base
  void omitted
  const expected: SessionRow[] = (['closed', 'idle', 'busy'] as const).map((state) => ({
    ...common, id: `mac:${id}`, session_id: id, host: 'mac', host_online: true, last_seen: 20,
    title: 'Remote', cwd: '/Users/me/dev/project', project: '~/dev/project', mtime: null,
    size: null, enabled: false, new: false, mutable: true, state,
    open_elsewhere: state === 'closed' ? [] : [{ status: state }],
    codex_state: state === 'busy' ? 'active' : 'notLoaded',
    resume_cmd: `cd -- /Users/me/dev/project && claude --resume ${id}`,
  }))
  expect(rowsFromPython(`
rows = [page_row(dict(id=sid, tool='claude', title='Remote', cwd='/Users/me/dev/project',
    mtime=None, size=None, state=state, mutable=True), 'mac', True, 20)
    for state in ('closed', 'idle', 'busy')]
print(json.dumps(rows))`)).toEqual(expected)
})
