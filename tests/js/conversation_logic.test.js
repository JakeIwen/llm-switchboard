// The conversation reducer, through `reduce` only. Run by
// tests/test_desk_conversation_js.py with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../../llm_switchboard/desk/static/conversation_logic.js");
const { PHASE, EV, FX, TIMER, SAY, REASON, CUE, LIMITS, COMMAND } = L;

const DEST = { host: "pi", session_id: "s1", generation: 3, label: "one", epoch: "e1" };

function target(over) {
  return Object.assign({ session_id: "s1", generation: 3, registration: "replied", lock: null }, over);
}
function snap(over) {
  return Object.assign({
    version: 1, epoch: "e1", sends_enabled: true, voice_confirm_enabled: true, lock_store_error: null,
    targets: [target()], requests: []
  }, over);
}
function rq(id, state, reply, session) {
  return { request_id: id, session_id: session || "s1", state: state, reply: reply || null };
}

// A conversation driven event by event, with its own clock and quietAt.
function machine() {
  let c = L.initial();
  const m = { now: 100000, quietAt: 0, fx: [] };
  m.ev = (type, fields) => {
    const r = L.reduce(c, Object.assign({ type: type, now: m.now, quietAt: m.quietAt }, fields || {}));
    c = r.c;
    m.fx = r.fx;
    return r.fx;
  };
  m.c = () => c;
  m.phase = () => c.phase;
  return m;
}

function find(fx, kind, props) {
  return fx.filter((f) => f.fx === kind && Object.keys(props || {}).every((k) => f[k] === props[k]));
}
function has(fx, kind, props) { return find(fx, kind, props).length > 0; }
function ended(m, reason) {
  assert.equal(m.phase(), PHASE.ENDED);
  assert.equal(m.c().reason, reason);
  assert.ok(has(m.fx, FX.END, { reason: reason }), JSON.stringify(m.fx));
}

// start -> listening, with a snapshot `s` taken at the start.
function listening(s) {
  const m = machine();
  m.ev(EV.START, { dest: DEST });
  m.ev(EV.SNAPSHOT_FETCHED, { snapshot: s || snap() });
  m.ev(EV.WAKE_OK);
  m.ev(EV.GRANT_OK, { startedAt: m.now, ctx: { selectedId: "s1", visible: true, voice: true } });
  assert.equal(m.phase(), PHASE.LISTENING);
  return m;
}
function say(m, text) { return m.ev(EV.RESULT, { text: text, final: true }); }

// listening -> sending -> waiting on request `id`.
function waiting(m, id) {
  say(m, "hello there");
  say(m, "send it");
  m.ev(EV.TICKET_OK, { ticket: "t1" });
  m.ev(EV.SEND_OK, { request_id: id || "r1", state: "handed_off" });
  assert.equal(m.phase(), PHASE.WAITING);
  return m;
}

test("commands are only the isolated phrases, any case and punctuation", () => {
  assert.equal(L.command("send it"), COMMAND.SEND);
  assert.equal(L.command(" Send it. "), COMMAND.SEND);
  assert.equal(L.command("SCRATCH THAT!"), COMMAND.SCRATCH);
  assert.equal(L.command("Read it back?"), COMMAND.READBACK);
  assert.equal(L.command("stop listening"), COMMAND.STOP);
  for (const text of ["and summarize them send it", "don't send it", "send it now", "send", "sent it",
    "please read it back", "stop", ""]) {
    assert.equal(L.command(text), null, text);
  }
});

test("the draft grows word for word and a result past 1,024 bytes clears it all", () => {
  assert.deepEqual(L.appendDraft("", "  hello "), { draft: "hello", overflow: false });
  assert.deepEqual(L.appendDraft("hello", "world"), { draft: "hello world", overflow: false });
  assert.deepEqual(L.appendDraft("x", ""), { draft: "x", overflow: false });
  const full = "a".repeat(1022);
  assert.equal(L.appendDraft(full, "b").draft.length, 1024);
  assert.deepEqual(L.appendDraft(full, "bc"), { draft: "", overflow: true });
  // Bytes, not characters: 342 three-byte characters are 1,026 bytes.
  assert.deepEqual(L.appendDraft("", "€".repeat(342)), { draft: "", overflow: true });
});

test("the text rule is the hub's", () => {
  assert.equal(L.draftProblem("hello"), null);
  assert.equal(L.draftProblem("/compact now"), "starts with a command character");
  assert.equal(L.draftProblem(""), "empty");
  assert.equal(L.draftProblem("a\u0007b"), "control character");
});

test("a start takes a snapshot, the wake lock and the grant, then opens the mic on a new turn", () => {
  const m = machine();
  assert.deepEqual(m.ev(EV.START, { dest: DEST }), [{ fx: FX.SNAPSHOT }]);
  assert.equal(m.phase(), PHASE.STARTING);
  assert.ok(has(m.ev(EV.SNAPSHOT_FETCHED, { snapshot: snap() }), FX.WAKE_LOCK));
  assert.ok(has(m.ev(EV.WAKE_OK), FX.GRANT));
  const fx = m.ev(EV.GRANT_OK, { startedAt: m.now, ctx: { selectedId: "s1", visible: true, voice: true } });
  assert.equal(m.phase(), PHASE.LISTENING);
  for (const [name, ms] of [[TIMER.TURN, LIMITS.TURN_MS], [TIMER.IDLE, LIMITS.IDLE_MS], [TIMER.WATCHDOG, LIMITS.WATCHDOG_MS]]) {
    assert.ok(has(fx, FX.TIMER, { name: name, ms: ms }), name);
  }
  assert.ok(has(fx, FX.LISTEN));
  // A second tap is ignored.
  assert.deepEqual(m.ev(EV.START, { dest: Object.assign({}, DEST, { session_id: "s2" }) }), []);
  assert.equal(m.c().dest.session_id, "s1");
});

test("a start ends when the target is not free or the switches are off", () => {
  const cases = [
    [snap({ targets: [target({ lock: { lock_id: "L", request_id: "x" } })] }), REASON.BUSY],
    [snap({ requests: [rq("x", "handed_off")] }), REASON.BUSY],
    [snap({ requests: [rq("x", "pending")] }), REASON.BUSY],
    [snap({ sends_enabled: false }), REASON.SENDS_OFF],
    [snap({ voice_confirm_enabled: false }), REASON.VOICE_OFF],
    [snap({ epoch: "e2" }), REASON.EPOCH],
    [snap({ targets: [target({ generation: 4 })] }), REASON.TARGET],
    [snap({ targets: [target({ registration: "ambiguous" })] }), REASON.TARGET],
    [snap({ targets: [] }), REASON.TARGET],
    [snap({ lock_store_error: "disk full" }), REASON.LOCK_STORE],
  ];
  for (const [s, reason] of cases) {
    const m = machine();
    m.ev(EV.START, { dest: DEST });
    m.ev(EV.SNAPSHOT_FETCHED, { snapshot: s });
    ended(m, reason);
    assert.ok(!has(m.fx, FX.WAKE_LOCK));
  }
});

test("the grant's checks use the page as it is when the grant arrives", () => {
  for (const [ctx, reason] of [
    [{ selectedId: "s2", visible: true, voice: true }, REASON.RETARGET],
    [{ selectedId: "s1", visible: false, voice: true }, REASON.HIDDEN],
    [{ selectedId: "s1", visible: true, voice: false }, REASON.NO_VOICE],
  ]) {
    const m = machine();
    m.ev(EV.START, { dest: DEST });
    m.ev(EV.SNAPSHOT_FETCHED, { snapshot: snap() });
    m.ev(EV.WAKE_OK);
    m.ev(EV.GRANT_OK, { startedAt: m.now, ctx: ctx });
    ended(m, reason);
    assert.ok(!has(m.fx, FX.LISTEN));
  }
  // A request that appeared after the start's snapshot.
  const m = machine();
  m.ev(EV.START, { dest: DEST });
  m.ev(EV.SNAPSHOT_FETCHED, { snapshot: snap() });
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("x", "answered")] }) });
  ended(m, REASON.FOREIGN);
});

test("a poll newer than the start's snapshot is checked against its history", () => {
  const m = machine();
  m.ev(EV.START, { dest: DEST });
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 5, requests: [rq("x", "answered")] }) });
  assert.equal(m.phase(), PHASE.STARTING);
  m.ev(EV.SNAPSHOT_FETCHED, { snapshot: snap({ version: 4 }) });
  ended(m, REASON.BUSY);
});

test("a failed start step ends it", () => {
  const m = machine();
  m.ev(EV.START, { dest: DEST });
  m.ev(EV.FAIL, { reason: REASON.HUB_UNREACHABLE });
  ended(m, REASON.HUB_UNREACHABLE);
});

test("no mic opens sooner than the guard after the last sound, at the start too", () => {
  const m = machine();
  m.quietAt = m.now - 100;
  m.ev(EV.START, { dest: DEST });
  m.ev(EV.SNAPSHOT_FETCHED, { snapshot: snap() });
  m.ev(EV.WAKE_OK);
  const fx = m.ev(EV.GRANT_OK, { startedAt: m.now, ctx: { selectedId: "s1", visible: true, voice: true } });
  assert.equal(m.phase(), PHASE.STARTING);
  assert.ok(has(fx, FX.TIMER, { name: TIMER.GUARD, ms: LIMITS.GUARD_MS - 100 }));
  assert.ok(!has(fx, FX.LISTEN));
  m.now += LIMITS.GUARD_MS - 100;
  assert.ok(has(m.ev(EV.TIMEOUT, { name: TIMER.GUARD }), FX.LISTEN));
  assert.equal(m.phase(), PHASE.LISTENING);
});

test("results build the draft; interim results only show", () => {
  const m = listening();
  assert.deepEqual(m.ev(EV.RESULT, { text: "hel", final: false }), [{ fx: FX.INTERIM, text: "hel" }]);
  say(m, "hello");
  say(m, "and summarize them send it");
  assert.equal(m.c().draft, "hello and summarize them send it");
  assert.equal(m.phase(), PHASE.LISTENING);
  assert.ok(has(m.fx, FX.TIMER, { name: TIMER.IDLE }));
});

test("send it with an empty draft is a cue, and the mic reopens on the cue's end", () => {
  const m = listening();
  const fx = say(m, "send it");
  assert.equal(m.phase(), PHASE.READING);
  assert.ok(has(fx, FX.STOP_LISTEN));
  assert.ok(has(fx, FX.SAY, { text: CUE.NOTHING_TO_SEND, kind: SAY.CUE }));
  m.quietAt = m.now;
  m.now += 600;
  const back = m.ev(EV.SAY_END, { kind: SAY.CUE });
  assert.equal(m.phase(), PHASE.LISTENING);
  assert.ok(has(back, FX.LISTEN));
  // Same turn: the turn deadline is not re-armed.
  assert.ok(!has(back, FX.TIMER, { name: TIMER.TURN }));
});

test("a draft the text rule refuses is a cue and stays", () => {
  const m = listening();
  say(m, "/compact");
  say(m, "send it");
  assert.equal(m.phase(), PHASE.READING);
  assert.ok(has(m.fx, FX.SAY, { text: CUE.CANT_SEND + ": starts with a command character.", kind: SAY.CUE }));
  assert.ok(!has(m.fx, FX.TICKET));
  assert.equal(m.c().draft, "/compact");
});

test("send it freezes the draft and leaves listening in the same step", () => {
  const m = listening();
  say(m, "list the files");
  const fx = say(m, "Send it.");
  assert.equal(m.phase(), PHASE.SENDING);
  assert.ok(has(fx, FX.STOP_LISTEN));
  assert.ok(has(fx, FX.TICKET));
  assert.ok(has(fx, FX.CLEAR_TIMER, { name: TIMER.TURN }));
  assert.ok(has(fx, FX.CLEAR_TIMER, { name: TIMER.IDLE }));
  assert.deepEqual(find(m.ev(EV.TICKET_OK, { ticket: "t1" }), FX.SEND), [{ fx: FX.SEND, ticket: "t1", text: "list the files" }]);
  const sent = m.ev(EV.SEND_OK, { request_id: "r1", state: "handed_off" });
  assert.equal(m.phase(), PHASE.WAITING);
  assert.equal(m.c().draft, "");
  assert.ok(has(sent, FX.SAY, { text: CUE.SENT, kind: SAY.SENT }));
  assert.ok(has(sent, FX.TIMER, { name: TIMER.ACK, ms: LIMITS.ACK_WAIT_MS }));
  assert.ok(has(sent, FX.TIMER, { name: TIMER.REPLY, ms: LIMITS.REPLY_WAIT_MS }));
  // The turn deadline is disarmed while waiting.
  m.ev(EV.TIMEOUT, { name: TIMER.TURN });
  assert.equal(m.phase(), PHASE.WAITING);
});

test("scratch that clears the draft with a cue", () => {
  const m = listening();
  say(m, "something wrong");
  say(m, "scratch that");
  assert.equal(m.c().draft, "");
  assert.ok(has(m.fx, FX.SAY, { text: CUE.CLEARED, kind: SAY.CUE }));
});

test("read it back speaks the exact draft and must complete", () => {
  const m = listening();
  say(m, "Delete the  temp files?");
  say(m, "read it back");
  assert.equal(m.phase(), PHASE.READING);
  assert.ok(has(m.fx, FX.SAY, { text: "Delete the  temp files?", kind: SAY.READBACK }));
  assert.ok(has(m.fx, FX.STOP_LISTEN));
  m.quietAt = m.now - LIMITS.GUARD_MS;
  m.ev(EV.SAY_END, { kind: SAY.READBACK });
  assert.equal(m.phase(), PHASE.LISTENING);
  assert.equal(m.c().draft, "Delete the  temp files?");

  const n = listening();
  say(n, "x");
  say(n, "read it back");
  n.ev(EV.SAY_FAILED);
  ended(n, REASON.SPEECH);
  assert.equal(n.c().draft, "");
});

test("stop listening ends and discards the draft", () => {
  const m = listening();
  say(m, "half a thought");
  say(m, "stop listening");
  ended(m, REASON.STOPPED);
  assert.equal(m.c().draft, "");
  // Nothing moves an ended conversation.
  assert.deepEqual(m.ev(EV.REC_END), []);
  assert.deepEqual(m.ev(EV.SAY_END, { kind: SAY.REPLY }), []);
});

test("a result past the limit clears the whole draft with a cue", () => {
  const m = listening();
  say(m, "a".repeat(1000));
  say(m, "b".repeat(30));
  assert.equal(m.c().draft, "");
  assert.equal(m.phase(), PHASE.READING);
  assert.ok(has(m.fx, FX.SAY, { text: CUE.OVERFLOW, kind: SAY.CUE }));
});

test("recognition restarts on end; no-speech is not a failure; other errors and quick loops end it", () => {
  const m = listening();
  m.now += 5100;
  assert.ok(has(m.ev(EV.REC_END), FX.LISTEN));
  m.now += 3000;
  assert.deepEqual(m.ev(EV.REC_ERROR, { error: "no-speech" }), []);
  assert.ok(has(m.ev(EV.REC_END), FX.LISTEN));
  m.ev(EV.REC_ERROR, { error: "network" });
  ended(m, L.withDetail(REASON.REC_FAILED, "network"));

  const q = listening();
  q.now += 200; q.ev(EV.REC_END);
  q.now += 200; q.ev(EV.REC_END);
  assert.equal(q.phase(), PHASE.LISTENING);
  q.now += 200; q.ev(EV.REC_END);
  ended(q, REASON.REC_LOOP);

  const s = listening();
  s.ev(EV.REC_START_FAILED);
  ended(s, REASON.REC_FAILED);
});

test("a start() with no audiostart before the watchdog ends it", () => {
  const m = listening();
  assert.ok(has(m.ev(EV.REC_AUDIOSTART), FX.CLEAR_TIMER, { name: TIMER.WATCHDOG }));
  const n = listening();
  n.ev(EV.TIMEOUT, { name: TIMER.WATCHDOG });
  ended(n, REASON.REC_HUNG);
});

test("the idle and turn limits end a turn, cues and read-backs included", () => {
  const m = listening();
  m.ev(EV.TIMEOUT, { name: TIMER.IDLE });
  ended(m, REASON.IDLE);
  const n = listening();
  say(n, "x");
  say(n, "read it back");
  n.ev(EV.TIMEOUT, { name: TIMER.TURN });
  ended(n, REASON.TURN);
});

test("freshness is checked before every mic opening and every final result", () => {
  const m = listening();
  m.now += LIMITS.FRESH_MS + 1;
  say(m, "hello");
  ended(m, REASON.HUB_LOST);
  assert.equal(m.c().lost, true);

  const n = listening();
  n.now += LIMITS.FRESH_MS + 1;
  n.ev(EV.REC_END);
  ended(n, REASON.HUB_LOST);

  const t = listening();
  const start = t.now;
  t.now += 4000;
  t.ev(EV.BEAT_OK, { startedAt: start + 3000 });
  t.now = start + 12000;
  t.ev(EV.TICK);
  assert.equal(t.phase(), PHASE.LISTENING);
  // An older beat's start never moves the deadline back.
  t.ev(EV.BEAT_OK, { startedAt: start + 1000 });
  t.now = start + 13001;
  t.ev(EV.TICK);
  ended(t, REASON.HUB_LOST);
});

test("past the freshness deadline no event acts or renews, before the tick has run", () => {
  const late = (m) => { m.now = m.c().freshUntil + 1; };
  const acting = [
    ["a beat success", (m) => m.ev(EV.BEAT_OK, { startedAt: m.now - 100 })],
    ["an interim result", (m) => m.ev(EV.RESULT, { text: "hel", final: false })],
    ["a final result", (m) => say(m, "send it")],
    ["a recognizer end", (m) => m.ev(EV.REC_END)],
    ["a guard timer", (m) => m.ev(EV.TIMEOUT, { name: TIMER.GUARD })],
    ["a poll", (m) => m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 9 }) })],
  ];
  for (const [name, act] of acting) {
    const m = listening();
    late(m);
    act(m);
    ended(m, REASON.HUB_LOST);
    assert.equal(m.c().lost, true, name);
    assert.ok(!m.fx.some((f) => f.fx === FX.LISTEN || f.fx === FX.SAY || f.fx === FX.TICKET), name);
  }
  // A ticket or send answer after the deadline posts and speaks nothing.
  const t = listening();
  say(t, "x");
  say(t, "send it");
  late(t);
  t.ev(EV.TICKET_OK, { ticket: "t1" });
  ended(t, REASON.HUB_LOST);
  assert.ok(!has(t.fx, FX.SEND));
  const s = listening();
  say(s, "x");
  say(s, "send it");
  s.ev(EV.TICKET_OK, { ticket: "t1" });
  late(s);
  s.ev(EV.SEND_OK, { request_id: "r1", state: "handed_off" });
  ended(s, REASON.HUB_LOST);
  assert.ok(!has(s.fx, FX.SAY));
  // A grant whose own request started more than FRESH_MS ago never opens the mic.
  const g = machine();
  g.ev(EV.START, { dest: DEST });
  g.ev(EV.SNAPSHOT_FETCHED, { snapshot: snap() });
  g.ev(EV.WAKE_OK);
  g.ev(EV.GRANT_OK, { startedAt: g.now - LIMITS.FRESH_MS - 1, ctx: { selectedId: "s1", visible: true } });
  ended(g, REASON.HUB_LOST);
  // The page's own end keeps its reason.
  const e = listening();
  late(e);
  e.ev(EV.END, { reason: REASON.END_TAPPED });
  ended(e, REASON.END_TAPPED);
});

test("waiting follows the exact request: absent before the ack, then answered", () => {
  const m = waiting(listening());
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2 }) });
  assert.equal(m.phase(), PHASE.WAITING);
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 3, requests: [rq("r1", "handed_off")], targets: [target({ lock: { lock_id: "L", request_id: "r1" } })] }) });
  assert.equal(m.phase(), PHASE.WAITING);
  m.ev(EV.TIMEOUT, { name: TIMER.ACK });
  assert.equal(m.phase(), PHASE.WAITING);
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 4, requests: [rq("r1", "answered", { text: "t", spoken: "All done." })] }) });
  // The "sent" cue is still playing.
  assert.equal(m.phase(), PHASE.WAITING);
  assert.ok(!has(m.fx, FX.SAY));
  m.ev(EV.SAY_END, { kind: SAY.SENT });
  assert.equal(m.phase(), PHASE.SPEAKING);
  assert.ok(has(m.fx, FX.SAY, { text: "All done.", kind: SAY.REPLY }));
  m.quietAt = m.now;
  m.now += LIMITS.GUARD_MS;
  const fx = m.ev(EV.SAY_END, { kind: SAY.REPLY });
  assert.equal(m.phase(), PHASE.LISTENING);
  assert.equal(m.c().draft, "");
  assert.ok(has(fx, FX.TIMER, { name: TIMER.TURN, ms: LIMITS.TURN_MS }));
});

test("a reply waits for the sent cue, and an empty spoken text is a fixed cue", () => {
  const m = waiting(listening());
  m.ev(EV.SAY_END, { kind: SAY.SENT });
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("r1", "answered", { text: "t", spoken: "  " })] }) });
  assert.equal(m.phase(), PHASE.SPEAKING);
  assert.ok(has(m.fx, FX.SAY, { text: CUE.NO_SPOKEN, kind: SAY.REPLY }));
  // Another poll with the same answer speaks nothing more.
  assert.ok(!has(m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 3, requests: [rq("r1", "answered", { text: "t", spoken: "  " })] }) }), FX.SAY));
});

test("waiting ends on every outcome that is not a reply", () => {
  const outcomes = [
    [[rq("r1", "answered")], REASON.NO_REPLY],
    [[rq("r1", "unknown")], REASON.UNKNOWN],
    [[rq("r1", "expired")], REASON.NO_REPLY],
    [[rq("r1", "handed_off"), rq("r9", "pending")], REASON.FOREIGN],
  ];
  for (const [requests, reason] of outcomes) {
    const m = waiting(listening());
    m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: requests }) });
    ended(m, reason);
  }
  // Seen once and then gone.
  const g = waiting(listening());
  g.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("r1", "handed_off")] }) });
  g.ev(EV.SNAPSHOT, { snapshot: snap({ version: 3 }) });
  ended(g, REASON.NO_REPLY);
  // An older poll that lacks it doesn't count as gone.
  const o = waiting(listening());
  o.ev(EV.SNAPSHOT, { snapshot: snap({ version: 5, requests: [rq("r1", "handed_off")] }) });
  o.ev(EV.SNAPSHOT, { snapshot: snap({ version: 4 }) });
  assert.equal(o.phase(), PHASE.WAITING);
  // Never seen within 30 s of the ack.
  const a = waiting(listening());
  a.ev(EV.TIMEOUT, { name: TIMER.ACK });
  ended(a, REASON.UNKNOWN);
  // Ten minutes without a reply.
  const r = waiting(listening());
  r.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("r1", "handed_off")] }) });
  r.ev(EV.TIMEOUT, { name: TIMER.REPLY });
  ended(r, REASON.REPLY_TIMEOUT);
});

test("an answer that beats the POST is spoken after the sent cue", () => {
  const m = listening();
  say(m, "quick one");
  say(m, "send it");
  m.ev(EV.TICKET_OK, { ticket: "t1" });
  // Our own request, answered, before the POST resolves: not foreign.
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("r1", "answered", { text: "t", spoken: "Yes." })] }) });
  assert.equal(m.phase(), PHASE.SENDING);
  m.ev(EV.SEND_OK, { request_id: "r1", state: "answered" });
  assert.equal(m.phase(), PHASE.WAITING);
  m.ev(EV.SAY_END, { kind: SAY.SENT });
  assert.ok(has(m.fx, FX.SAY, { text: "Yes.", kind: SAY.REPLY }));
});

test("the end conditions hold while sending, and refusals end it without a retry", () => {
  const m = listening();
  say(m, "x");
  say(m, "send it");
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, voice_confirm_enabled: false }) });
  ended(m, REASON.VOICE_OFF);

  for (const [type, fields, reason] of [
    [EV.SEND_REFUSED, { reason: "busy" }, L.withDetail(REASON.NOT_SENT, "busy")],
    [EV.SEND_UNKNOWN, {}, REASON.UNKNOWN],
    [EV.SEND_OK, { request_id: "r1", state: "unknown" }, REASON.UNKNOWN],
  ]) {
    const n = listening();
    say(n, "x");
    say(n, "send it");
    n.ev(EV.TICKET_OK, { ticket: "t" });
    n.ev(type, fields);
    ended(n, reason);
    assert.ok(!n.fx.some((f) => f.fx === FX.SEND || f.fx === FX.TICKET));
  }
  const t = listening();
  say(t, "x");
  say(t, "send it");
  t.ev(EV.TICKET_FAIL, { reason: "too many tickets" });
  ended(t, L.withDetail(REASON.NOT_SENT, "too many tickets"));
});

test("foreign activity seen before the POST is issued ends it, and nothing is posted", () => {
  const m = listening();
  say(m, "x");
  say(m, "send it");
  // The ticket is still outstanding: a foreign request starts and finishes.
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("f1", "answered", { text: "t", spoken: "s" })] }) });
  ended(m, REASON.FOREIGN);
  assert.deepEqual(m.ev(EV.TICKET_OK, { ticket: "t1" }), []);

  const l = listening();
  say(l, "x");
  say(l, "send it");
  l.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, targets: [target({ lock: { lock_id: "L", request_id: "f1" } })] }) });
  ended(l, REASON.FOREIGN);
});

test("while the POST is unresolved every request and lock seen is kept and judged when it settles", () => {
  // A foreign request seen in one poll and gone from the next still counts.
  const m = listening();
  say(m, "x");
  say(m, "send it");
  m.ev(EV.TICKET_OK, { ticket: "t1" });
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("f1", "pending")] }) });
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 3, requests: [rq("r1", "handed_off")] }) });
  assert.equal(m.phase(), PHASE.SENDING);
  m.ev(EV.SEND_OK, { request_id: "r1", state: "handed_off" });
  ended(m, REASON.FOREIGN);
  assert.ok(!has(m.fx, FX.SAY));

  // So does a lock tied to another request, seen once.
  const l = listening();
  say(l, "x");
  say(l, "send it");
  l.ev(EV.TICKET_OK, { ticket: "t1" });
  l.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, targets: [target({ lock: { lock_id: "L", request_id: "f1" } })] }) });
  l.ev(EV.SNAPSHOT, { snapshot: snap({ version: 3 }) });
  l.ev(EV.SEND_OK, { request_id: "r1", state: "handed_off" });
  ended(l, REASON.FOREIGN);

  // Our own request and its lock, seen before the POST answered, are not foreign.
  const o = listening();
  say(o, "x");
  say(o, "send it");
  o.ev(EV.TICKET_OK, { ticket: "t1" });
  o.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("r1", "handed_off")], targets: [target({ lock: { lock_id: "L", request_id: "r1" } })] }) });
  o.ev(EV.SEND_OK, { request_id: "r1", state: "handed_off" });
  assert.equal(o.phase(), PHASE.WAITING);
});

test("foreign activity during a turn ends it and discards the draft", () => {
  const m = listening(snap({ requests: [rq("old", "answered", { text: "t", spoken: "s" })] }));
  say(m, "half");
  // History stays history, whatever its state.
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, requests: [rq("old", "answered", { text: "t", spoken: "s" })] }) });
  assert.equal(m.phase(), PHASE.LISTENING);
  // Requests on other targets are not ours to judge.
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 3, requests: [rq("z", "pending", null, "s2")] }) });
  assert.equal(m.phase(), PHASE.LISTENING);
  m.ev(EV.SNAPSHOT, { snapshot: snap({ version: 4, requests: [rq("new", "answered", { text: "t", spoken: "s" })] }) });
  ended(m, REASON.FOREIGN);
  assert.equal(m.c().draft, "");
  const l = listening();
  l.ev(EV.SNAPSHOT, { snapshot: snap({ version: 2, targets: [target({ lock: { lock_id: "L", request_id: "x" } })] }) });
  ended(l, REASON.FOREIGN);
});

test("two spoken turns on a target that already had an answered request", () => {
  const old = rq("r0", "answered", { text: "t", spoken: "Old reply." });
  const m = listening(snap({ requests: [old] }));
  let v = 2;
  for (const id of ["r1", "r2"]) {
    waiting(m, id);
    m.ev(EV.SAY_END, { kind: SAY.SENT });
    const reqs = [old, rq("r1", "answered", { text: "t", spoken: "Reply r1." })];
    if (id === "r2") reqs.push(rq("r2", "answered", { text: "t", spoken: "Reply r2." }));
    m.ev(EV.SNAPSHOT, { snapshot: snap({ version: v++, requests: reqs }) });
    assert.ok(has(m.fx, FX.SAY, { text: "Reply " + id + ".", kind: SAY.REPLY }));
    m.quietAt = m.now - LIMITS.GUARD_MS;
    m.ev(EV.SAY_END, { kind: SAY.REPLY });
    assert.equal(m.phase(), PHASE.LISTENING);
  }
});

test("an end from the page ends any phase, and a lost grant is marked", () => {
  const m = listening();
  m.ev(EV.END, { reason: REASON.GRANT_LOST, lost: true });
  ended(m, REASON.GRANT_LOST);
  assert.equal(m.c().lost, true);
  const s = machine();
  s.ev(EV.START, { dest: DEST });
  s.ev(EV.END, { reason: REASON.END_TAPPED });
  ended(s, REASON.END_TAPPED);
  // A new tap starts afresh.
  assert.deepEqual(s.ev(EV.START, { dest: DEST }), [{ fx: FX.SNAPSHOT }]);
  assert.equal(s.c().reason, null);
});
