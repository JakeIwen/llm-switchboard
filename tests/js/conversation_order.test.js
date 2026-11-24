// An answer that silences the page is applied before any timer already
// overdue: some overdue timers start things (the guard opens the mic, the
// idle, turn, reply-wait and watchdog limits speak a closing cue), and once
// the page knows it must go silent none of them may run on the way to the
// end. Each case holds the event loop across a timer's due time, so no fake
// timer runs, and then delivers the silencing answer: no recognizer is
// made or started, nothing is submitted to speech synthesis, and the
// conversation ends with its grant released. Through the real page wiring
// (page_harness.js). Run by tests/test_desk_conversation_js.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { G } = require("./grant_ids.js");
const {
  L, PATH, REASON, POLL, settle, paired, snap, voiceRq, ended, releases, listen, last, drafted, repaired, tapRefresh,
  abandonedSnapshot
} = require("./page_harness.js");
const { CALLS } = require("./page_calls.js");
const { LIMITS } = L;

const failed = (status) => L.withDetail(REASON.CALL_FAILED, "HTTP " + status);
// A snapshot of the hub the running conversation started on (`h.epoch`, set
// by a row whose conversation started after a new hub epoch).
const now = (h, over) => snap(Object.assign({ epoch: h.epoch || "e1" }, over));
const gone = (h) => now(h, { targets: [] });

// The grant the running conversation holds: the newest one a grant request was answered with.
function held(h) {
  const g = h.all(PATH.GRANT).filter((c) => c.sent && c.sent.grant).pop();
  assert.ok(g, "no grant was given");
  return g.sent.grant;
}

// Time passing with the hub alive up to `until`: every beat renewed and
// every poll answered (`shows()` makes its snapshot), except the call under
// test (`keep`); `poke` hears a word every two minutes, so the idle limit
// never comes.
async function live(h, until, o) {
  o = o || {};
  let poked = h.t;
  while (h.t < until) {
    await h.advance(Math.min(1000, until - h.t));
    const grant = held(h);
    for (const b of h.pending(PATH.BEAT)) if (b !== o.keep) { b.answer(200, { grant, expires_in: 15 }); await settle(); }
    for (const p of h.pending(POLL)) if (p !== o.keep) { p.answer(200, (o.shows || now)(h)); await settle(); }
    if (o.poke && h.t - poked >= 120000) { h.rec().hear("more"); await settle(); poked = h.t; }
  }
}

// A voice send of a listening conversation, handed off as request r7, its "Sent." cue over.
async function handedOff(h) {
  h.rec().hear("check the logs");
  h.rec().hear("send it");
  await settle();
  last(h, PATH.TICKET).answer(200, { ticket: "t7" });
  await settle();
  last(h, PATH.SEND).answer(200, { request_id: "r7", state: "handed_off" });
  await settle();
  h.utt().fire("end");
  await settle();
}

const waitingFor = (h) => now(h, { requests: [voiceRq("r7", "handed_off")] });

// Every timer whose callback can start something: `prep` brings it on, from
// a listening conversation, without answering the call under test, and
// `ms` is how long after that it is due.
const ARMS = {
  "the guard before a resumed turn's mic": {
    ms: LIMITS.GUARD_MS,
    prep: async (h) => { h.rec().hear("read it back"); await settle(); h.utt().fire("end"); await settle(); }
  },
  "the guard before a new turn's mic": {
    ms: LIMITS.GUARD_MS,
    prep: async (h, keep) => {
      await handedOff(h);
      const p = h.pending(POLL).filter((x) => x !== keep)[0];
      assert.ok(p, "no poll to bring the reply");
      p.answer(200, now(h, { requests: [voiceRq("r7", "answered", "the reply")] }));
      await settle();
      assert.equal(h.utt().text, "the reply");
      h.utt().fire("end");
      await settle();
    }
  },
  "the idle limit": { ms: LIMITS.IDLE_MS, prep: async (h) => { h.rec().hear("check"); await settle(); } },
  "the turn limit": { ms: LIMITS.TURN_MS, prep: async () => {}, poke: true },
  "the reply-wait limit": { ms: LIMITS.REPLY_WAIT_MS, prep: handedOff, shows: waitingFor },
  "the recognizer's start watchdog": {
    ms: LIMITS.WATCHDOG_MS,
    prep: async (h) => { h.deaf = true; h.rec().onend(); await settle(); }
  }
};

// Brings timer `name` to 20 ms before it is due, the call `keep` still
// open; with `before`, so that it falls due 10 ms before that time.
// Returns when it is due.
async function arm(h, name, keep, before) {
  const a = ARMS[name];
  if (before !== undefined) await live(h, before - 10 - a.ms, { keep });
  await a.prep(h, keep);
  const at = h.dueOf(a.ms);
  await live(h, at - 20, { keep, shows: a.shows, poke: a.poke });
  assert.ok(!keep || !keep.done, "the call under test was closed while the timer came due");
  assert.equal(h.state().startsWith("Conversation ended"), false, "the conversation ended while the timer came due");
  return at;
}

const fail503 = async (h, call) => { await h.headers(call, 503); };
const lose = async (h, call) => { await (await h.headers(call, 200)).lose(); };
const answer = (status, data) => async (h, call) => {
  call.answer(status, typeof data === "function" ? data(h) : data);
  await settle();
};
const poll = (h) => h.pending(POLL).filter((p) => !p.earlier)[0];

// Every event that silences the page: how to have its call open with a
// conversation listening (`open`, returning the call, or null when the
// carrier is the poll open at delivery), and how it arrives (`deliver`).
// `deadline(call)`: the answer comes after that call's own limit, which falls
// due in the same stall.
const ROWS = {};
const SENDING_OR_STARTING = ["the Start snapshot", "the grant request", "the voice ticket", "the voice send"];
for (const [name, modes] of Object.entries(CALLS)) {
  if (SENDING_OR_STARTING.includes(name)) continue;
  ROWS[`${name}: a 503 at its status line`] = { open: modes.running, deliver: fail503 };
}
// The conversation an abandoned grant request is left behind by starts on a new hub epoch.
const onNewEpoch = (open) => async (h) => { const call = await open(h); h.epoch = "e2"; return call; };
ROWS["an abandoned grant request: a 503 at its status line"].open =
  onNewEpoch(CALLS["an abandoned grant request"].running);
Object.assign(ROWS, {
  "the startup listing's 200 whose body is lost": { open: CALLS["the startup listing"].running, deliver: lose },
  "the poll's 200 whose body is lost": { open: listen, deliver: lose },
  "the poll's 200 with no snapshot": { open: listen, deliver: answer(200, null) },
  "the poll showing the target gone": { open: listen, deliver: answer(200, gone) },
  "the poll showing an older snapshot with the target gone": {
    open: listen, deliver: answer(200, (h) => now(h, { version: 1, targets: [] }))
  },
  "the tap flow's refresh showing the target gone": {
    open: async (h) => { const r = await tapRefresh(h); await listen(h); return r; }, deliver: answer(200, gone)
  },
  "an abandoned Start snapshot showing the target gone": {
    open: async (h) => { const s = await abandonedSnapshot(h); await listen(h); return s; }, deliver: answer(200, gone)
  },
  "an earlier pairing's poll showing the target gone": {
    open: async (h) => { const p = await repaired(h); p.earlier = true; await listen(h); return p; },
    deliver: answer(200, gone)
  },
  "the beat's 200 that renews nothing": { open: CALLS["the beat"].running, deliver: answer(200, {}) },
  "an abandoned grant request's 200": {
    open: onNewEpoch(CALLS["an abandoned grant request"].running), deliver: answer(200, { grant: G.g1, expires_in: 15 })
  },
  "an abandoned voice ticket's 200 with no ticket": {
    open: CALLS["an abandoned voice ticket"].running, deliver: answer(200, {})
  },
  "an earlier conversation's send's 200 that is no hand-off": {
    open: CALLS["an earlier conversation's voice send"].running, deliver: answer(200, {})
  },
  "an abandoned voice ticket's ticket after the call's own limit": {
    open: CALLS["an abandoned voice ticket"].running, deliver: answer(200, { ticket: "t9" }),
    deadline: (call) => call.at + LIMITS.STEP_MS
  },
  "the beat's renewal after the call's own limit": {
    open: CALLS["the beat"].running, deliver: answer(200, { grant: G.g1, expires_in: 15 }),
    deadline: (call) => call.at + LIMITS.BEAT_ABORT_MS
  }
});

const LONG = ["the idle limit", "the turn limit", "the reply-wait limit"];

// The cells this table does not drive, and why: rows (by their start) and
// the timers they aren't crossed with. Most are cells no page can reach.
// The poll rows against the long limits are a limit of this fixture, which
// opens its call before the timer is armed: on a page a poll opened shortly
// before one of those limits can be answered just after it, and that answer
// takes the same path through heard() as the other status rows.
const UNREACHABLE = [
  { rows: /^the poll: /, timers: ["the guard before a new turn's mic", "the reply-wait limit"],
    why: "the open poll is the call that would bring the reply, or show the request the wait needs" },
  { rows: /^(the poll: |an earlier pairing's poll)/, timers: LONG,
    why: "the fixture's poll is open from before the limit is armed, and the page aborts a poll after 35 s" },
  { rows: /^a startup acknowledgement/, timers: LONG, why: "the page gives up on an acknowledgement first" },
  { rows: /^an earlier conversation's (voice )?send/, timers: LONG, why: "the call's own 20 s limit aborts it first" },
  { rows: /^an abandoned voice ticket's ticket after/, timers: LONG,
    why: "the answer comes after the call's own 10 s limit, and the timer takes longer than that to come due" },
  { rows: /^the beat/, timers: LONG.concat(["the recognizer's start watchdog"]),
    why: "with the current beat open no other renews the grant, which lapses 10 s after its last renewal" }
];

function unreachable(row, timer) {
  return UNREACHABLE.some((u) => u.rows.test(row) && u.timers.includes(timer));
}

for (const [row, r] of Object.entries(ROWS)) {
  for (const timer of Object.keys(ARMS)) {
    if (unreachable(row, timer)) continue;
    test(`${row}, with ${timer} overdue: no mic, no speech, a silent end`, async () => {
      const h = await paired();
      const call = (await r.open(h)) || null;
      assert.equal(h.state(), "Listening.");
      const at = await arm(h, timer, call, r.deadline ? r.deadline(call) : undefined);
      h.t = Math.max(at, r.deadline ? r.deadline(call) : 0) + 20;
      const before = h.counts();
      const grant = held(h);
      await r.deliver(h, call || poll(h));
      assert.deepEqual(h.counts(), before, "a recognizer was made or started, or an utterance submitted");
      assert.match(h.state(), /^Conversation ended: /);
      assert.ok(releases(h).includes(grant), "the grant was not released");
    });
  }
}

// The same, case by case, with what the end shows.

test("503 headers with the post-speech guard 20 ms overdue make and start no recognizer", async () => {
  const h = await paired();
  await listen(h);
  const at = await arm(h, "the guard before a resumed turn's mic", null);
  h.t = at + 20;
  const before = h.counts();
  await h.headers(poll(h), 503);
  assert.deepEqual(h.counts(), before);
  assert.equal(h.state(), ended(failed(503)));
  assert.deepEqual(releases(h), [G.g1]);
});

test("a 200 whose body read fails with the post-speech guard 20 ms overdue makes and starts no recognizer", async () => {
  const h = await paired();
  await listen(h);
  const at = await arm(h, "the guard before a resumed turn's mic", null);
  h.t = at + 20;
  const before = h.counts();
  await lose(h, poll(h));
  assert.deepEqual(h.counts(), before);
  assert.equal(h.state(), ended(REASON.UNUSABLE));
  assert.deepEqual(releases(h), [G.g1]);
});

test("a 503 at an overdue idle limit submits no utterance", async () => {
  const h = await paired();
  await listen(h);
  const at = await arm(h, "the idle limit", null);
  h.t = at + 20;
  const said = h.said.length;
  await h.headers(poll(h), 503);
  assert.equal(h.said.length, said, "an utterance was submitted");
  assert.equal(h.state(), ended(failed(503)));
  assert.deepEqual(releases(h), [G.g1]);
});

test("a ticket's 429 after freshness lapsed, before the overdue tick ran, shows the status, then its body", async () => {
  const h = await paired();
  await listen(h);
  const acquired = h.t;
  await h.advance(8000);
  assert.equal(h.pending(PATH.BEAT).length, 1, "the beat is not open");
  await drafted(h);
  const ticket = h.pending(PATH.TICKET)[0];
  assert.ok(ticket, "no ticket was asked for");
  await h.advance(acquired + LIMITS.FRESH_MS - 100 - h.t);
  const said = h.said.length;
  h.t = acquired + 11100;
  const body = await h.headers(ticket, 429);
  assert.equal(h.state(), ended(failed(429)));
  await body.arrive({ error: "ticket table full" });
  assert.equal(h.state(), ended(L.withDetail(REASON.NOT_SENT, "ticket table full")));
  assert.equal(h.said.length, said, "the page spoke");
  assert.deepEqual(releases(h), [G.g1]);
  assert.equal(h.all(PATH.SEND).length, 0, "the prompt was posted");
});
