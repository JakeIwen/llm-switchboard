import { test } from "vitest";
import assert from "node:assert/strict";
import { compileQuery, transcriptQuery, transcriptIndexStatus, IS_VALUES } from "./sessionQuery";

import type { SessionRow, SessionStatus } from "../api/sessionRow";

const NOW = 1_000_000;
const S = (o: Partial<Omit<SessionRow, 'status'>> & { status?: Partial<SessionStatus> | null }): SessionRow => ({
  title: "", first_prompt: "", project: "", cwd: "", id: "", rc_name: "", enabled: false,
  open_elsewhere: [], mtime: NOW - 60, tool: "claude", host: "", short_id: "", size: null,
  remote_url: "", resume_cmd: "", new: false, ...o,
  status: o.status ? { session_id: "", state: "starting", detail: "", window_id: null, pid: null,
    rss_kb: null, rc_active: false, busy: false, duplicate: false, started_at: null,
    next_attempt: 0, failures: 0, launches: 0, last_exit: null, rc_name: "", ...o.status } : null,
});
const sessions = [
  S({ id: "c96934f7-aaaa", title: "Telemetrics app changes", project: "~/dev/obd-things", cwd: "/home/pi/dev/obd-things",
      enabled: true, status: { state: "running", busy: true } }),
  S({ id: "0c598c4d-bbbb", title: "Reply with just OK", project: "/tmp", cwd: "/tmp", mtime: NOW - 10 * 86400 }),
  S({ id: "5b80da2b-cccc", title: "Fix the CAN bus decoder", project: "~/dev/obd-things", cwd: "/home/pi/dev/obd-things",
      open_elsewhere: [{ pid: 1, status: "idle" }] }),
];
const ids = (q: string) => sessions.filter((s) => compileQuery(q).test(s, NOW)).map((s) => s.id.slice(0, 4));

test("transcript bridge translates tool filters and explains discarded session-only terms", () => {
  assert.deepEqual(transcriptQuery('CAN is:claude -IS:"CODEX" is:kept -is:busy state:running title:"two words" id:abc'), {
    query: "CAN tool:claude -tool:codex",
    ignored: ["is:kept", "-is:busy", "state:running", 'title:"two words"', "id:abc"],
  });
});

test("transcript bridge preserves shared syntax, quoted literals and unknown prefixes", () => {
  const query = '"is:kept" -"two words" dir:alpha folder:beta in:gamma age:<2d https://host by:me after:2026-01-01';
  assert.deepEqual(transcriptQuery(query), { query, ignored: [] });
  assert.deepEqual(transcriptQuery("is:kept -state: title: id:"), {
    query: "", ignored: ["is:kept", "-state:", "title:", "id:"],
  });
  assert.deepEqual(transcriptQuery("  \t "), { query: "", ignored: [] });
});

test("plain words are ANDed and case-insensitive", () => {
  assert.deepEqual(ids("obd"), ["c969", "5b80"]);
  assert.deepEqual(ids("OBD telemetrics"), ["c969"]);
  assert.deepEqual(ids(""), ["c969", "0c59", "5b80"]);
});

test("minus negates", () => {
  assert.deepEqual(ids("-telemetrics"), ["0c59", "5b80"]);
  assert.deepEqual(ids("obd -can"), ["c969"]);
  assert.deepEqual(ids("-dir:tmp"), ["c969", "5b80"]);
  assert.deepEqual(ids('-"just ok"'), ["c969", "5b80"]);
  assert.deepEqual(ids('-"ok just"'), ["c969", "0c59", "5b80"]);
  assert.deepEqual(ids("obd -"), ["c969", "5b80"]);
  assert.deepEqual(ids("obd -dir:"), ["c969", "5b80"]);
});

test("quoted phrases", () => {
  assert.deepEqual(ids('"bus decoder"'), ["5b80"]);
  assert.deepEqual(ids('"decoder bus"'), []);
  assert.deepEqual(ids('"unterminated phr'), []);
});

test("field prefixes", () => {
  assert.deepEqual(ids("dir:~/dev"), ["c969", "5b80"]);
  assert.deepEqual(ids("folder:/home/pi"), ["c969", "5b80"]);
  assert.deepEqual(ids("title:ok"), ["0c59"]);
  assert.deepEqual(ids("id:5b80"), ["5b80"]);
  assert.deepEqual(ids("is:kept"), ["c969"]);
  assert.deepEqual(ids("is:off"), ["0c59", "5b80"]);
  assert.deepEqual(ids("is:running"), ["c969"]);
  assert.deepEqual(ids("is:open"), ["c969", "5b80"]);
  assert.deepEqual(ids("is:elsewhere"), ["5b80"]);
  assert.deepEqual(ids("is:busy"), ["c969"]);
  assert.deepEqual(ids("state:running"), ["c969"]);
});

test("age comparisons", () => {
  assert.deepEqual(ids("age:<1d"), ["c969", "5b80"]);
  assert.deepEqual(ids("age:>1w"), ["0c59"]);
  assert.deepEqual(ids("age:2w"), ["c969", "0c59", "5b80"]);
  assert.deepEqual(ids("-age:<1h"), ["0c59"]);
});

test("unknown prefixes are plain text; bad values report errors", () => {
  assert.deepEqual(ids("https://x"), []);
  assert.deepEqual(compileQuery("is:nope").errors.length, 1);
  assert.deepEqual(compileQuery("age:soon").errors.length, 1);
  assert.equal(compileQuery("  - ").empty, true);
});

test("is:codex and is:claude pick the tool; a working Codex session is busy", () => {
  const mixed = [S({ id: "aaaa", tool: "claude" }), S({ id: "bbbb", tool: "codex", codex_state: "active" }),
                 S({ id: "cccc", tool: "codex", codex_state: "notLoaded" })];
  const pick = (q: string) => mixed.filter((s) => compileQuery(q).test(s, NOW)).map((s) => s.id);
  assert.deepEqual(pick("is:codex"), ["bbbb", "cccc"]);
  assert.deepEqual(pick("is:claude"), ["aaaa"]);
  assert.deepEqual(pick("is:codex is:busy"), ["bbbb"]);
  assert.deepEqual(pick("is:codex is:open"), []);
});

test("age comparisons cover equality, fractions, units and unknown timestamps", () => {
  const row = S({ mtime: NOW - 60 });
  for (const query of ['age:<=1m', 'age:>=1m', 'age:<1h', 'age:>0.5m', 'age:1d', 'age:<1w']) {
    assert.equal(compileQuery(query).test(row, NOW), true, query);
  }
  for (const query of ['age:<1m', 'age:>1m', 'age:<=0.5m', 'age:>=2m']) {
    assert.equal(compileQuery(query).test(row, NOW), false, query);
  }
  for (const mtime of [0, null]) {
    assert.equal(compileQuery('age:<1d').test(S({ mtime }), NOW), false);
    assert.equal(compileQuery('-age:<1d').test(S({ mtime }), NOW), true);
  }
});

test("remaining aliases, first prompt, rc name, attention and external busy processes match", () => {
  const row = S({ first_prompt: 'Unique prompt', rc_name: 'Remote Name', enabled: true,
    status: { state: 'attention' }, open_elsewhere: [{ pid: 8, status: 'busy' }] });
  for (const query of ['unique', '"remote name"', 'title:remote', 'is:on', 'is:attention', 'is:busy']) {
    assert.equal(compileQuery(query).test(row, NOW), true, query);
  }
  assert.deepEqual(ids('in:obd'), ['c969', '5b80']);
  assert.equal(compileQuery('is:attention').test(S({}), NOW), false);
  assert.equal(compileQuery('is:running').test(S({ status: { state: 'running' } }), NOW), false);
});

test("errors are non-filtering and empty quotes and half-typed prefixes retain legacy behavior", () => {
  const query = compileQuery('is:nope age:soon');
  assert.equal(query.empty, true);
  assert.deepEqual(query.errors, [`is: takes ${IS_VALUES.join(', ')}`, 'age: looks like <2d, >1w, <30m']);
  assert.equal(query.test(S({}), NOW), true);
  assert.equal(compileQuery('""').empty, true);
  assert.equal(compileQuery('dir:').empty, false);
  assert.deepEqual(ids('dir:'), []);
  assert.deepEqual(ids('-dir:'), ['c969', '0c59', '5b80']);
  assert.equal(compileQuery('is:constructor').test(S({}), NOW), true);
  assert.throws(() => compileQuery('is:__proto__').test(S({}), NOW), TypeError);
});

test("all query results and bridges match the old page on a branch-covering matrix", async () => {
  const url = new URL('../../../llm_switchboard/static/search.js', import.meta.url).href;
  const legacy = await import(url);
  const inputs = [...sessions, S({ first_prompt: 'https://host', rc_name: 'Remote', mtime: null }),
    S({ enabled: true, status: { state: 'attention' } }),
    S({ tool: 'codex', codex_state: 'active' }), S({ open_elsewhere: [{ status: 'busy' }] })];
  const queries = ['', '-', '""', 'dir:', '-dir:', 'is:', 'is:constructor', 'https://host', 'title:remote',
    'unique -nope', 'age:<=1m', 'age:>=1m', 'age:<1m', 'age:>1w', 'age:soon',
    ...IS_VALUES.map((value) => `is:${value}`), 'state:attention', 'is:nope',
    'CAN is:claude -IS:"CODEX" title:"two words"', 'constructor:foo', 'in:obd'];
  for (const query of queries) {
    const actual = compileQuery(query), previous = legacy.compileQuery(query);
    assert.deepEqual(actual.errors, previous.errors, query);
    assert.equal(actual.empty, previous.empty, query);
    assert.deepEqual(transcriptQuery(query), legacy.transcriptQuery(query), query);
    for (const row of inputs) assert.equal(actual.test(row, NOW), previous.test(row, NOW), query);
  }
});

test("transcript index status retains every fulltext status branch", () => {
  assert.equal(transcriptIndexStatus(null, NOW), 'Transcript index: checking…');
  assert.equal(transcriptIndexStatus({ state: 'building', sessions: 1, last_updated: null }, NOW), 'Transcript index: updating · 1 session');
  assert.equal(transcriptIndexStatus({ state: 'ready', sessions: 2, last_updated: NOW - 300 }, NOW), 'Transcript index: 2 sessions · updated 5m ago');
  assert.equal(transcriptIndexStatus({ state: 'ready', sessions: 0, last_updated: null }, NOW), 'Transcript index: 0 sessions · updated just now');
  assert.equal(transcriptIndexStatus({ state: 'error', sessions: 0, last_updated: null, error: 'failed' }, NOW), 'Transcript index error: failed');
  assert.equal(transcriptIndexStatus({ state: 'error', sessions: 0, last_updated: null }, NOW), 'Transcript index error: unknown error');
  assert.equal(transcriptIndexStatus({ state: 'missing', sessions: 0, last_updated: null }, NOW), 'Transcript index: not built yet');
  for (const state of ['future', 'constructor', '__proto__']) {
    assert.equal(transcriptIndexStatus({ state: state as 'missing', sessions: 0, last_updated: null }, NOW), 'Transcript index: not built yet');
  }
});
