// The conversation controller wired to fake recognition, speech, timers,
// wake lock and fetch, through its public surface only. Run by
// tests/test_desk_conversation_js.py with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../../llm_switchboard/desk/static/conversation_logic.js");
const C = require("../../llm_switchboard/desk/static/conversation.js");
const { PHASE, REASON, CUE, LIMITS } = L;
const { PATH } = C;

const DEST = { host: "pi", session_id: "s1", generation: 3, label: "one", epoch: "e1" };
const flush = () => new Promise((resolve) => setImmediate(resolve));

function target(over) {
  return Object.assign({ session_id: "s1", generation: 3, registration: "replied", lock: null }, over);
}
let version = 100;
function snap(over) {
  return Object.assign({
    version: ++version, epoch: "e1", sends_enabled: true, voice_confirm_enabled: true, lock_store_error: null,
    targets: [target()], requests: []
  }, over);
}
function rq(id, state, spoken) {
  return { request_id: id, session_id: "s1", state: state, reply: spoken === undefined ? null : { text: "t", spoken: spoken } };
}

function harness() {
  const h = {
    t: 1000, timers: [], handle: 0, recs: [], said: [], calls: [], selected: "s1", visible: true, voice: true,
    autoAudio: true, honorAbort: true, recThrows: false, sentinels: [], wakeFails: false
  };
  class FakeRecognition {
    constructor() { this.starts = 0; this.aborts = 0; h.recs.push(this); }
    start() {
      if (h.recThrows) throw new Error("not allowed");
      this.starts++;
      if (h.autoAudio) queueMicrotask(() => this.fire("audiostart"));
    }
    abort() { this.aborts++; }
    fire(name, ev) { if (this["on" + name]) this["on" + name](ev || {}); }
    result(text, final) {
      const result = [{ transcript: text }];
      result.isFinal = final !== false;
      this.fire("result", { resultIndex: 0, results: [result] });
    }
  }
  class FakeUtterance {
    constructor(text) { this.text = text; this.listeners = {}; }
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
    fire(name, ev) {
      if (name === "end" || name === "error") h.synth.speaking = false;
      if (this["on" + name]) this["on" + name](ev || {});
      (this.listeners[name] || []).forEach((fn) => fn(ev || {}));
    }
  }
  h.synth = {
    speaking: false, pending: false, cancels: 0,
    speak(u) { h.said.push(u); this.speaking = true; },
    cancel() { this.cancels++; this.speaking = false; }
  };
  function api(method, path, body, opts) {
    return new Promise((resolve, reject) => {
      const call = { method, path, body, done: false };
      call.answer = (status, data) => { call.done = true; resolve({ status, data }); };
      call.fail = () => { call.done = true; reject(new Error("network")); };
      if (opts && opts.signal && h.honorAbort) {
        opts.signal.addEventListener("abort", () => { call.aborted = true; call.done = true; reject(new Error("abort")); });
      }
      h.calls.push(call);
    });
  }
  const env = {
    Recognition: FakeRecognition, Utterance: FakeUtterance, synth: h.synth, AbortController: AbortController,
    now: () => h.t,
    setTimeout: (fn, ms) => { const id = ++h.handle; h.timers.push({ id, at: h.t + ms, fn }); return id; },
    clearTimeout: (id) => { h.timers = h.timers.filter((x) => x.id !== id); },
    wakeLock: {
      request: () => {
        if (h.wakeFails) return Promise.reject(new Error("denied"));
        const s = { released: 0, listeners: [] };
        s.addEventListener = (name, fn) => s.listeners.push(fn);
        s.release = () => { s.released++; return Promise.resolve(); };
        h.sentinels.push(s);
        return Promise.resolve(s);
      }
    }
  };
  h.Utterance = FakeUtterance;
  h.ctl = C.create({
    api, env,
    deps: { selectedId: () => h.selected, visible: () => h.visible, voiceAvailable: () => h.voice, onChange: () => {} }
  });
  h.view = () => h.ctl.view();
  h.phase = () => h.ctl.view().phase;
  h.rec = () => h.recs[h.recs.length - 1];
  h.utt = () => h.said[h.said.length - 1];
  h.pending = (path) => h.calls.filter((x) => x.path === path && !x.done);
  h.all = (path) => h.calls.filter((x) => x.path === path);
  h.answer = async (path, status, data) => {
    const call = h.pending(path)[0];
    assert.ok(call, "no pending call to " + path);
    call.answer(status, data);
    await flush();
    return call;
  };
  // Runs every timer due within `ms`, in order, settling promises between them.
  h.advance = async (ms) => {
    const end = h.t + ms;
    for (;;) {
      const due = h.timers.filter((x) => x.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      h.timers = h.timers.filter((x) => x !== due);
      h.t = due.at;
      due.fn();
      await flush();
    }
    h.t = end;
    await flush();
  };
  // Time passing with the hub answering every beat.
  h.alive = async (ms) => {
    for (let left = ms; left > 0; left -= 500) {
      await h.advance(Math.min(500, left));
      for (const call of h.pending(PATH.BEAT)) { call.answer(200, { grant: "g1", expires_in: 15 }); await flush(); }
    }
  };
  // The current utterance plays to its end.
  h.finish = async () => { h.utt().fire("end"); await flush(); };
  h.hear = async (text) => { h.rec().result(text); await flush(); };
  return h;
}

async function listening(h, s) {
  assert.equal(h.ctl.start(DEST), true);
  await flush();
  await h.answer(PATH.STATE, 200, s || snap());
  await h.answer(PATH.GRANT, 200, { grant: "g1", expires_in: 15 });
  assert.equal(h.phase(), PHASE.LISTENING);
  assert.equal(h.rec().starts, 1);
  return h;
}

// From listening: draft, "send it", ticket, POST answered with request `id`.
async function sent(h, id, text) {
  await h.hear(text || "check the logs");
  await h.hear("send it");
  assert.equal(h.phase(), PHASE.SENDING);
  const ticket = await h.answer(PATH.TICKET, 200, { ticket: "t-" + id });
  assert.deepEqual(ticket.body, { session_id: "s1", generation: 3, epoch: "e1" });
  const post = await h.answer(PATH.SEND, 200, { request_id: id, state: "handed_off" });
  assert.deepEqual(post.body, { ticket: "t-" + id, text: text || "check the logs", confirm: "voice", grant: "g1" });
  assert.equal(h.phase(), PHASE.WAITING);
  assert.equal(h.utt().text, CUE.SENT);
}

function texts(h) { return h.said.map((u) => u.text); }
function releases(h) { return h.all(PATH.RELEASE).map((x) => x.body.grant); }

test("a stale callback inside the current turn is dropped", async () => {
  const h = await listening(harness());
  const first = h.rec();
  await h.advance(2000);
  first.fire("end");
  await flush();
  const second = h.rec();
  assert.notEqual(second, first);
  first.result("hello");
  first.result("send it");
  first.fire("end");
  first.fire("audiostart");
  await flush();
  assert.equal(h.view().draft, "");
  assert.equal(h.phase(), PHASE.LISTENING);
  assert.equal(h.recs.length, 2);
  assert.equal(h.all(PATH.TICKET).length, 0);
});

test("the end of an aborted recognizer never reopens the mic", async () => {
  const h = await listening(harness());
  await h.hear("open the hatch");
  await h.hear("read it back");
  const aborted = h.recs[0];
  assert.equal(aborted.aborts, 1);
  assert.equal(h.phase(), PHASE.READING);
  aborted.fire("error", { error: "aborted" });
  aborted.fire("end");
  await flush();
  assert.equal(h.recs.length, 1);
  assert.equal(h.utt().text, "open the hatch");
  await h.finish();
  // The read-back ended: the mic opens only after the guard.
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.recs.length, 1);
  await h.advance(1);
  assert.equal(h.recs.length, 2);
  assert.equal(h.phase(), PHASE.LISTENING);
  assert.equal(h.view().draft, "open the hatch");
});

test("a ticket arriving after the end is ignored", async () => {
  const h = harness();
  h.honorAbort = false;
  await listening(h);
  await h.hear("x");
  await h.hear("send it");
  h.ctl.endTap();
  assert.equal(h.view().reason, REASON.END_TAPPED);
  await h.answer(PATH.TICKET, 200, { ticket: "late" });
  assert.equal(h.all(PATH.SEND).length, 0);
});

test("a grant arriving after End during starting is released at once", async () => {
  const h = harness();
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  h.ctl.endTap();
  assert.equal(h.phase(), PHASE.ENDED);
  assert.equal(h.sentinels[0].released, 1);
  await h.answer(PATH.GRANT, 200, { grant: "late-grant" });
  assert.deepEqual(releases(h), ["late-grant"]);
  assert.equal(h.recs.length, 0);
  assert.equal(h.said.length, 0);
});

test("a retarget during starting ends it, at the tap or when the grant arrives", async () => {
  const h = harness();
  h.honorAbort = false;
  h.ctl.start(DEST);
  await flush();
  h.ctl.targetSelected("s1");
  assert.equal(h.phase(), PHASE.STARTING);
  h.ctl.targetSelected("s2");
  assert.equal(h.view().reason, REASON.RETARGET);
  await h.answer(PATH.STATE, 200, snap());
  assert.equal(h.sentinels.length, 0);

  // Selection changed without a notice: the grant's own check catches it,
  // and the page, still holding a fresh grant, says why and then releases it.
  const g = harness();
  g.ctl.start(DEST);
  await flush();
  await g.answer(PATH.STATE, 200, snap());
  g.selected = "s2";
  await g.answer(PATH.GRANT, 200, { grant: "g1" });
  assert.equal(g.view().reason, REASON.RETARGET);
  assert.equal(g.recs.length, 0);
  assert.deepEqual(texts(g), [CUE.ENDED + ": " + REASON.RETARGET + "."]);
  assert.deepEqual(releases(g), []);
  assert.equal(g.ctl.canStart(), false);
  await g.finish();
  assert.deepEqual(releases(g), ["g1"]);
});

test("a reply during the sent cue waits for the cue's end", async () => {
  const h = await listening(harness());
  await sent(h, "r1");
  h.ctl.snapshot(snap({ requests: [rq("r1", "answered", "It is done.")] }));
  await flush();
  assert.deepEqual(texts(h), [CUE.SENT]);
  await h.finish();
  assert.equal(h.phase(), PHASE.SPEAKING);
  assert.deepEqual(texts(h), [CUE.SENT, "It is done."]);
});

test("a poll that would speak a reply twice speaks it once", async () => {
  const h = await listening(harness());
  await sent(h, "r1");
  await h.finish();
  const answered = () => snap({ requests: [rq("r1", "answered", "Once only.")] });
  h.ctl.snapshot(answered());
  h.ctl.snapshot(answered());
  await flush();
  assert.deepEqual(texts(h), [CUE.SENT, "Once only."]);
  await h.finish();
  await h.advance(LIMITS.GUARD_MS);
  assert.equal(h.phase(), PHASE.LISTENING);
  h.ctl.snapshot(answered());
  await flush();
  assert.deepEqual(texts(h), [CUE.SENT, "Once only."]);
});

test("a late beat success does not renew the grant", async () => {
  const h = harness();
  h.honorAbort = false;
  await listening(h);
  await h.advance(LIMITS.BEAT_MS);
  const late = h.pending(PATH.BEAT)[0];
  assert.ok(late);
  await h.advance(LIMITS.BEAT_ABORT_MS + 500);
  late.answer(200, { grant: "g1" });
  await flush();
  // Fresh until 10 s after the grant request; the next beat never answers.
  await h.advance(LIMITS.FRESH_MS - LIMITS.BEAT_MS - LIMITS.BEAT_ABORT_MS - 500);
  assert.equal(h.phase(), PHASE.LISTENING);
  await h.advance(LIMITS.TICK_MS);
  assert.equal(h.view().reason, REASON.HUB_LOST);
  assert.equal(h.view().lost, true);
  // A page that lost its grant says nothing and still lets go of it.
  assert.equal(h.said.length, 0);
  assert.deepEqual(releases(h), ["g1"]);
});

test("a beat success that arrives after the deadline revives nothing", async () => {
  const h = await listening(harness());
  // The first beat is abandoned unanswered; the second starts at the deadline.
  await h.advance(LIMITS.FRESH_MS);
  assert.equal(h.phase(), PHASE.LISTENING);
  const second = h.pending(PATH.BEAT);
  assert.equal(second.length, 1);
  h.t += 100;
  second[0].answer(200, { grant: "g1", expires_in: 15 });
  await flush();
  assert.equal(h.view().reason, REASON.HUB_LOST);
  assert.equal(h.view().lost, true);
  assert.equal(h.rec().aborts, 1);
  assert.equal(h.said.length, 0);
});

test("an interim result after the deadline ends it before the tick runs", async () => {
  const h = await listening(harness());
  h.t += LIMITS.FRESH_MS + 1;
  h.rec().result("hel", false);
  await flush();
  assert.equal(h.view().reason, REASON.HUB_LOST);
  assert.equal(h.view().interim, "");
});

test("a refused beat means the grant is gone", async () => {
  const h = await listening(harness());
  await h.advance(LIMITS.BEAT_MS);
  await h.answer(PATH.BEAT, 409, { error: "conversation grant not current" });
  assert.equal(h.view().reason, REASON.GRANT_LOST);
  assert.equal(h.said.length, 0);
  assert.equal(h.rec().aborts, 1);
});

test("the request absent then present is followed to its reply", async () => {
  const h = await listening(harness());
  await sent(h, "r1");
  await h.finish();
  h.ctl.snapshot(snap());
  await h.alive(5000);
  assert.equal(h.phase(), PHASE.WAITING);
  h.ctl.snapshot(snap({ requests: [rq("r1", "handed_off")], targets: [target({ lock: { lock_id: "L", request_id: "r1" } })] }));
  await h.alive(LIMITS.ACK_WAIT_MS);
  assert.equal(h.phase(), PHASE.WAITING);
  h.ctl.snapshot(snap({ requests: [rq("r1", "answered", "Found it.")] }));
  await flush();
  assert.equal(h.utt().text, "Found it.");

  // Never present within 30 s of the ack: unknown.
  const n = await listening(harness());
  await sent(n, "r1");
  await n.finish();
  await n.alive(LIMITS.ACK_WAIT_MS);
  assert.equal(n.view().reason, REASON.UNKNOWN);
});

test("an answer that beats the POST is spoken after the sent cue", async () => {
  const h = await listening(harness());
  await h.hear("quick question");
  await h.hear("send it");
  await h.answer(PATH.TICKET, 200, { ticket: "t" });
  h.ctl.snapshot(snap({ requests: [rq("r1", "answered", "Yes.")] }));
  await flush();
  assert.equal(h.phase(), PHASE.SENDING);
  await h.answer(PATH.SEND, 200, { request_id: "r1", state: "answered" });
  assert.deepEqual(texts(h), [CUE.SENT]);
  await h.finish();
  assert.deepEqual(texts(h), [CUE.SENT, "Yes."]);
});

test("a foreign request seen while the ticket is outstanding ends it before any POST", async () => {
  const h = harness();
  h.honorAbort = false;
  await listening(h);
  await h.hear("x");
  await h.hear("send it");
  h.ctl.snapshot(snap({ requests: [rq("f1", "answered", "Someone else.")] }));
  await flush();
  assert.equal(h.view().reason, REASON.FOREIGN);
  await h.answer(PATH.TICKET, 200, { ticket: "t" });
  assert.equal(h.all(PATH.SEND).length, 0);
});

test("a send with no answer is unknown and never retried", async () => {
  const h = await listening(harness());
  await h.hear("x");
  await h.hear("send it");
  await h.answer(PATH.TICKET, 200, { ticket: "t" });
  await h.alive(LIMITS.SEND_DEADLINE_MS);
  assert.equal(h.view().reason, REASON.UNKNOWN);
  assert.equal(h.all(PATH.SEND).length, 1);
  assert.equal(h.all(PATH.TICKET).length, 1);

  const r = await listening(harness());
  await r.hear("x");
  await r.hear("send it");
  await r.answer(PATH.TICKET, 200, { ticket: "t" });
  await r.answer(PATH.SEND, 409, { error: "conversation grant not current" });
  assert.equal(r.view().reason, "not sent: conversation grant not current");
});

test("an utterance that never ends is cancelled, ends it, and holds Start for the quiet period", async () => {
  const h = await listening(harness());
  await h.hear("one two three four five");
  await h.hear("read it back");
  const deadline = L.estimateMs("one two three four five") * L.SPEECH_DEADLINE_FACTOR;
  await h.alive(deadline - 500);
  assert.equal(h.phase(), PHASE.READING);
  await h.alive(500);
  assert.equal(h.view().reason, REASON.SPEECH);
  assert.equal(h.synth.cancels, 1);
  // Cut off: no closing cue; the grant is let go at once.
  assert.equal(h.said.length, 1);
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.ctl.canStart(), false);
  assert.equal(h.ctl.start(DEST), false);
  await h.advance(deadline - 1);
  assert.equal(h.ctl.canStart(), false);
  await h.advance(1);
  assert.equal(h.ctl.canStart(), true);
});

test("a draft overflow clears the whole draft with a cue", async () => {
  const h = await listening(harness());
  await h.hear("a".repeat(1000));
  await h.hear("b".repeat(30));
  assert.equal(h.view().draft, "");
  assert.equal(h.phase(), PHASE.READING);
  assert.equal(h.recs[0].aborts, 1);
  assert.equal(h.utt().text, CUE.OVERFLOW);
  await h.finish();
  await h.advance(LIMITS.GUARD_MS);
  assert.equal(h.phase(), PHASE.LISTENING);
  await h.hear("send it");
  assert.equal(h.utt().text, CUE.NOTHING_TO_SEND);
});

test("a target with an answered request completes two spoken turns", async () => {
  const h = await listening(harness(), snap({ requests: [rq("r0", "answered", "Old reply.")] }));
  const history = [rq("r0", "answered", "Old reply.")];
  for (const id of ["r1", "r2"]) {
    await sent(h, id, "question " + id);
    await h.finish();
    history.push(rq(id, "answered", "Reply " + id + "."));
    h.ctl.snapshot(snap({ requests: history.slice() }));
    await flush();
    assert.equal(h.utt().text, "Reply " + id + ".");
    await h.finish();
    await h.advance(LIMITS.GUARD_MS);
    assert.equal(h.phase(), PHASE.LISTENING);
    assert.equal(h.view().draft, "");
  }
  assert.deepEqual(texts(h), [CUE.SENT, "Reply r1.", CUE.SENT, "Reply r2."]);
});

test("new foreign activity during a turn ends it, closes the mic and says why", async () => {
  const h = await listening(harness());
  await h.hear("half a thought");
  h.ctl.snapshot(snap({ requests: [rq("r9", "answered", "Someone else.")] }));
  await flush();
  assert.equal(h.view().reason, REASON.FOREIGN);
  assert.equal(h.view().draft, "");
  assert.equal(h.rec().aborts, 1);
  assert.deepEqual(texts(h), [CUE.ENDED + ": " + REASON.FOREIGN + "."]);
  await h.finish();
  assert.deepEqual(releases(h), ["g1"]);
});

test("hide, return and Start while an utterance is playing is refused until it must be over", async () => {
  const h = await listening(harness());
  await sent(h, "r1");
  await h.finish();
  h.ctl.snapshot(snap({ requests: [rq("r1", "answered", "A long reply that is still playing.")] }));
  await flush();
  assert.equal(h.phase(), PHASE.SPEAKING);
  h.visible = false;
  h.ctl.hidden();
  assert.equal(h.view().reason, REASON.HIDDEN);
  assert.equal(h.synth.cancels, 1);
  assert.deepEqual(releases(h), ["g1"]);
  h.visible = true;
  assert.equal(h.ctl.start(DEST), false);
  assert.equal(h.phase(), PHASE.ENDED);
  // The cut utterance's own end never arrives; its quiet period does.
  await h.advance(L.estimateMs("A long reply that is still playing.") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(h.ctl.start(DEST), true);
});

test("a start() with no audiostart is ended by the watchdog", async () => {
  const h = harness();
  h.autoAudio = false;
  await listening(h);
  await h.alive(LIMITS.WATCHDOG_MS);
  assert.equal(h.view().reason, REASON.REC_HUNG);
  assert.equal(h.rec().aborts, 1);
});

test("a silent session ends with nomatch then end, and recognition restarts", async () => {
  const h = await listening(harness());
  await h.alive(5100);
  h.rec().fire("nomatch");
  h.rec().fire("end");
  await flush();
  assert.equal(h.recs.length, 2);
  assert.equal(h.recs[1].starts, 1);
  assert.equal(h.phase(), PHASE.LISTENING);
});

test("a no-speech error followed by end restarts recognition", async () => {
  const h = await listening(harness());
  await h.alive(3000);
  h.rec().fire("error", { error: "no-speech" });
  h.rec().fire("end");
  await flush();
  assert.equal(h.recs.length, 2);
  assert.equal(h.phase(), PHASE.LISTENING);
  h.rec().fire("error", { error: "network" });
  await flush();
  assert.equal(h.view().reason, "speech recognition failed: network");
});

test("a recognizer that refuses to start ends it", async () => {
  const h = harness();
  h.recThrows = true;
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.answer(PATH.GRANT, 200, { grant: "g1" });
  assert.equal(h.view().reason, REASON.REC_FAILED);
});

test("Start right after a completed closing cue waits out the guard", async () => {
  const h = await listening(harness());
  await h.hear("stop listening");
  assert.deepEqual(texts(h), [CUE.ENDED + ": " + REASON.STOPPED + "."]);
  assert.equal(h.ctl.canStart(), false);
  await h.finish();
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.ctl.start(DEST), true);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.answer(PATH.GRANT, 200, { grant: "g2" });
  assert.equal(h.phase(), PHASE.STARTING);
  assert.equal(h.recs.length, 1);
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.recs.length, 1);
  await h.advance(1);
  assert.equal(h.recs.length, 2);
  assert.equal(h.phase(), PHASE.LISTENING);
});

test("the page's own utterances hold Start and set the guard", async () => {
  const h = harness();
  const u = new h.Utterance("Play this reply.");
  h.ctl.track(u);
  assert.equal(h.ctl.canStart(), false);
  u.fire("end");
  assert.equal(h.ctl.canStart(), true);
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.answer(PATH.GRANT, 200, { grant: "g1" });
  assert.equal(h.recs.length, 0);
  await h.advance(LIMITS.GUARD_MS);
  assert.equal(h.recs.length, 1);

  // Cancelled by the page: uncertain for twice its estimate.
  const g = harness();
  const v = new g.Utterance("Cut me off.");
  g.ctl.track(v);
  v.fire("error", { error: "interrupted" });
  assert.equal(g.ctl.canStart(), false);
  await g.advance(L.estimateMs("Cut me off.") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(g.ctl.canStart(), true);
});

// Starts a conversation whose grant answers at the current instant.
async function startUntilGrant(h, grantId) {
  assert.equal(h.ctl.start(DEST), true);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  assert.equal(h.pending(PATH.GRANT).length, 1);
  return async () => h.answer(PATH.GRANT, 200, { grant: grantId || "g1" });
}

test("a Play that outlives its deadline is cancelled, and its real end still holds the next mic for the guard", async () => {
  const h = harness();
  // One long number is one word to the estimator.
  const u = new h.Utterance("4815162342");
  h.ctl.track(u);
  const deadline = L.estimateMs(u.text) * L.SPEECH_DEADLINE_FACTOR;
  await h.advance(deadline);
  assert.equal(h.synth.cancels, 1);
  assert.equal(h.ctl.canStart(), false);
  await h.advance(deadline);
  assert.equal(h.ctl.canStart(), true);
  const grantArrives = await startUntilGrant(h);
  // Its real end comes now, at the very instant the grant arrives.
  u.fire("end");
  await grantArrives();
  assert.equal(h.recs.length, 0);
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.recs.length, 0);
  await h.advance(1);
  assert.equal(h.recs.length, 1);
  assert.equal(h.phase(), PHASE.LISTENING);
});

test("a conversation utterance's real end after its cut still holds the next mic for the guard", async () => {
  const h = await listening(harness());
  await h.hear("one two");
  await h.hear("read it back");
  const readback = h.utt();
  const deadline = L.estimateMs("one two") * L.SPEECH_DEADLINE_FACTOR;
  await h.alive(deadline);
  assert.equal(h.view().reason, REASON.SPEECH);
  await h.advance(deadline);
  assert.equal(h.ctl.canStart(), true);
  const grantArrives = await startUntilGrant(h, "g2");
  readback.fire("end");
  await grantArrives();
  assert.equal(h.recs.length, 1);
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.recs.length, 1);
  await h.advance(1);
  assert.equal(h.recs.length, 2);
});

test("a conversation utterance's error cancels the voice as cleanup and holds Start", async () => {
  const h = await listening(harness());
  await h.hear("one two");
  await h.hear("read it back");
  h.utt().fire("error", { error: "synthesis-failed" });
  await flush();
  assert.equal(h.view().reason, REASON.SPEECH);
  assert.equal(h.synth.cancels, 1);
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.ctl.canStart(), false);
  await h.advance(L.estimateMs("one two") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(h.ctl.canStart(), true);
});

test("the wake lock released by the system ends it", async () => {
  const h = await listening(harness());
  h.sentinels[0].listeners.forEach((fn) => fn());
  await flush();
  assert.equal(h.view().reason, REASON.WAKE);

  const n = harness();
  n.wakeFails = true;
  n.ctl.start(DEST);
  await flush();
  await n.answer(PATH.STATE, 200, snap());
  assert.equal(n.view().reason, REASON.NO_WAKE);
  assert.equal(n.all(PATH.GRANT).length, 0);
});

test("a refused grant ends the start with the hub's reason and speaks nothing", async () => {
  const h = harness();
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.answer(PATH.GRANT, 409, { error: "another page is paired; revoke its token first" });
  assert.equal(h.view().reason, REASON.REFUSED + ": another page is paired; revoke its token first");
  assert.equal(h.said.length, 0);
  assert.equal(h.sentinels[0].released, 1);
});

test("a hung start step fails, and its late grant is still released", async () => {
  const h = harness();
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.advance(LIMITS.STEP_MS);
  assert.equal(h.view().reason, REASON.HUB_UNREACHABLE);
  await h.answer(PATH.GRANT, 200, { grant: "slow" });
  assert.deepEqual(releases(h), ["slow"]);
});

test("unpairing ends it as a lost grant, with no closing cue", async () => {
  const h = await listening(harness());
  h.ctl.unpaired();
  assert.equal(h.view().reason, REASON.UNPAIRED);
  assert.equal(h.said.length, 0);
});
