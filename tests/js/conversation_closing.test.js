// The closing cue's rule, through the controller and the page: the hub's
// answers can only silence the page. For every call the conversation makes,
// every answer that is not that call's validated success starts no closing
// cue, and cuts one that is playing (speech no longer playing, the grant
// released, nothing spoken in its place); its validated success leaves the
// cue alone. Run by tests/test_desk_conversation_js.py with `node --test`;
// that file also drives the real hub's refusals through refusal_probe.js.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { G } = require("./grant_ids.js");
const L = require("../../llm_switchboard/desk/static/conversation_logic.js");
const C = require("../../llm_switchboard/desk/static/conversation.js");
const { PHASE, REASON, CUE, LIMITS } = L;
const { PATH } = C;
const { DEST, flush, target, snap, harness, listening, texts, releases } = require("./controller_harness.js");
const P = require("./page_harness.js");

// Every kind of answer that is not a call's success, for a call whose
// success is a 200 of some shape: refusals (known, unknown, wordless), the
// right words at the wrong status, malformed bodies and lost bodies.
const FAILURES = [
  [409, { error: "busy" }], [429, { error: "ticket table full" }], [409, { error: "conversation grant not current" }],
  [409, { error: "a refusal from a newer hub" }], [403, { error: "sends off" }], [500, { error: "internal error" }],
  [200, { error: "busy" }], [503, { error: "busy" }],
  [200, {}], [200, { ticket: 7, grant: 7, request_id: 7 }], [200, "busy"], [200, []],
  [200, null], [409, null], [503, null], [502, null],
];
const what = (status, data) => status + " " + JSON.stringify(data);
const cueText = (h) => texts(h).filter((t) => t.startsWith(CUE.ENDED));

// No closing cue started, nothing playing, the grant let go.
function noCue(h, label) {
  assert.deepEqual(cueText(h), [], label);
  assert.equal(h.synth.speaking, false, label);
  assert.deepEqual(releases(h), [G.g1], label);
}

// The cue that was playing is no longer playing, nothing replaced it, and
// the grant was released at once.
function cut(h, said, label) {
  assert.equal(h.synth.speaking, false, label + ": the closing cue is still playing");
  assert.equal(h.said.length, said, label + ": something was spoken in its place");
  assert.deepEqual(releases(h), [G.g1], label);
  assert.equal(h.ctl.holds(), false, label);
}

// A closing cue the page decided (End), playing with the grant held.
function playing(h, label) {
  assert.equal(h.synth.speaking, true, label);
  assert.ok(h.utt().text.startsWith(CUE.ENDED + ": " + REASON.END_TAPPED), label + " " + h.utt().text);
  assert.deepEqual(releases(h), [], label);
  return h.said.length;
}

async function sending(posted, opts) {
  const h = harness();
  if (opts && opts.keepTicket) h.honorAbort = false;
  await listening(h);
  await h.hear("check the logs");
  await h.hear("send it");
  if (posted) await h.answer(PATH.TICKET, 200, { ticket: "t" });
  return h;
}

// Each call: how to reach it with the conversation running, and with the
// closing cue playing (End tapped while it was out), and how to answer it.
const CALLS = {
  ticket: {
    active: () => sending(false),
    closing: async () => { const h = await sending(false, { keepTicket: true }); h.ctl.endTap(); return h; },
    answer: (h, status, data) => h.answer(PATH.TICKET, status, data),
    success: [200, { ticket: "t" }]
  },
  send: {
    active: () => sending(true),
    closing: async () => { const h = await sending(true); h.ctl.endTap(); return h; },
    answer: (h, status, data) => h.answer(PATH.SEND, status, data),
    success: [200, { request_id: "r1", state: "handed_off" }]
  },
  beat: {
    active: async () => { const h = await listening(harness()); await h.advance(LIMITS.BEAT_MS); return h; },
    closing: async () => {
      const h = await listening(harness());
      h.ctl.endTap();
      await h.advance(LIMITS.BEAT_MS);
      return h;
    },
    answer: (h, status, data) => h.answer(PATH.BEAT, status, data),
    success: [200, { grant: G.g1, expires_in: 15 }]
  }
};

for (const [name, call] of Object.entries(CALLS)) {
  test("a " + name + " answer that is not its success starts no closing cue", async () => {
    for (const [status, data] of FAILURES) {
      const h = await call.active();
      await call.answer(h, status, data);
      assert.equal(h.phase(), PHASE.ENDED, name + " " + what(status, data));
      noCue(h, name + " " + what(status, data));
    }
  });

  test("a " + name + " answer that is not its success cuts the closing cue playing", async () => {
    for (const [status, data] of FAILURES) {
      const h = await call.closing();
      const said = playing(h, name + " " + what(status, data));
      await call.answer(h, status, data);
      cut(h, said, name + " " + what(status, data));
    }
    // Its validated success leaves the cue to finish.
    const ok = await call.closing();
    const said = playing(ok, name + " success");
    await call.answer(ok, ...call.success);
    assert.equal(ok.synth.speaking, true, name + " success cut the cue");
    assert.equal(ok.said.length, said);
    assert.deepEqual(releases(ok), []);
    await ok.finish();
    assert.deepEqual(releases(ok), [G.g1]);
  });
}

// Snapshots that make the target unusable, for any reason, or that are no snapshot at all.
const BAD_SNAPSHOTS = [
  ["unenrolled at the same generation", () => snap({ targets: [target({ enrolled: false })] })],
  ["unregistered", () => snap({ targets: [target({ registration: "none" })] })],
  ["ambiguous", () => snap({ targets: [target({ registration: "ambiguous" })] })],
  ["another generation", () => snap({ targets: [target({ generation: 4 })] })],
  ["the target gone", () => snap({ targets: [] })],
  ["another epoch", () => snap({ epoch: "e2" })],
  ["sends off", () => snap({ sends_enabled: false })],
  ["voice confirm off", () => snap({ voice_confirm_enabled: false })],
  ["the lock store failed", () => snap({ lock_store_error: "lock file full" })],
  ["no targets list", () => ({ version: 999, epoch: "e1", sends_enabled: true, voice_confirm_enabled: true })],
  ["a target entry that is no record", () => snap({ targets: [null] })],
  ["not a snapshot", () => ({})],
];

test("a snapshot that makes the target unusable starts no closing cue", async () => {
  for (const [name, s] of BAD_SNAPSHOTS) {
    const h = await listening(harness());
    await h.hear("half a thought");
    h.ctl.snapshot(s());
    await flush();
    assert.equal(h.phase(), PHASE.ENDED, name);
    noCue(h, name);
  }
});

test("a snapshot that makes the target unusable cuts the closing cue playing; a healthy one doesn't", async () => {
  for (const [name, s] of BAD_SNAPSHOTS) {
    const h = await listening(harness());
    h.ctl.endTap();
    const said = playing(h, name);
    h.ctl.snapshot(s());
    await flush();
    cut(h, said, name);
  }
  const ok = await listening(harness());
  ok.ctl.endTap();
  const said = playing(ok, "healthy");
  ok.ctl.snapshot(snap());
  await flush();
  assert.equal(ok.synth.speaking, true);
  assert.equal(ok.said.length, said);
  assert.deepEqual(releases(ok), []);
});

test("a refused grant acquisition starts no cue, and an abandoned one's answer cuts a later closing cue", async () => {
  for (const [status, data] of FAILURES) {
    const h = harness();
    h.ctl.start(DEST);
    await flush();
    await h.answer(PATH.STATE, 200, snap());
    await h.answer(PATH.GRANT, status, data);
    assert.equal(h.phase(), PHASE.ENDED, what(status, data));
    assert.equal(h.said.length, 0, what(status, data));
    assert.equal(h.synth.speaking, false, what(status, data));
  }
  // A request given up on, let go when the hub's epoch changed, answers
  // during the next conversation's closing cue: refused or granted, the cue stops.
  for (const [status, data] of [...FAILURES, [200, { grant: G.late, expires_in: 15 }]]) {
    const h = harness();
    h.ctl.start(DEST);
    await flush();
    await h.answer(PATH.STATE, 200, snap());
    await h.advance(LIMITS.STEP_MS);
    h.ctl.snapshot(snap({ epoch: "e2" }));
    assert.equal(h.ctl.canStart(), true, what(status, data));
    h.ctl.start(Object.assign({}, DEST, { epoch: "e2" }));
    await flush();
    await h.answer(PATH.STATE, 200, snap({ epoch: "e2" }));
    const [old, mine] = h.pending(PATH.GRANT);
    mine.answer(200, { grant: G.g1, expires_in: 15 });
    await flush();
    assert.equal(h.phase(), PHASE.LISTENING);
    h.ctl.endTap();
    const said = playing(h, what(status, data));
    old.answer(status, data);
    await flush();
    assert.equal(h.synth.speaking, false, what(status, data) + ": the closing cue is still playing");
    assert.equal(h.said.length, said, what(status, data));
    assert.ok(releases(h).includes(G.g1), what(status, data));
  }
});

test("an end the hub's silence brings is silent, and the page's own ends still speak", async () => {
  const s = await sending(true);
  await s.alive(LIMITS.SEND_DEADLINE_MS);
  assert.equal(s.view().reason, REASON.UNKNOWN);
  noCue(s, "send deadline");
  const f = await listening(harness());
  await f.advance(LIMITS.FRESH_MS + LIMITS.TICK_MS);
  assert.equal(f.view().reason, REASON.HUB_LOST);
  noCue(f, "lapsed grant");
  const cases = [
    ["stop listening", async (h) => { await h.hear("stop listening"); }, REASON.STOPPED],
    ["End", async (h) => { h.ctl.endTap(); }, REASON.END_TAPPED],
    ["the recognizer failed", async (h) => { h.rec().fire("error", { error: "network" }); await flush(); },
      L.withDetail(REASON.REC_FAILED, "network")],
    ["the idle limit", async (h) => { await h.alive(LIMITS.IDLE_MS); }, REASON.IDLE],
  ];
  for (const [name, act, reason] of cases) {
    const h = await listening(harness());
    await act(h);
    assert.deepEqual(cueText(h), [CUE.ENDED + ": " + reason + "."], name);
    assert.equal(h.synth.speaking, true, name);
  }
});

// The page's own poll, through app.js: an answer that is not a snapshot
// (another status, a body that never came) cuts the closing cue too.
test("a poll answered with anything but a snapshot cuts the closing cue", async () => {
  for (const [status, data, lose] of [[503, { error: "busy" }], [500, null], [200, null], [200, undefined, true]]) {
    const label = status + " " + JSON.stringify(data) + (lose ? " lost" : "");
    const h = await P.paired();
    await P.converse(h);
    h.$("conv-end").click();
    await P.settle();
    assert.equal(h.utt().text, P.ended(REASON.END_TAPPED), label);
    assert.equal(h.synth.speaking, true, label);
    const said = h.said.length;
    if (lose) await (await h.headers(P.POLL, status)).lose();
    else await h.answer(P.POLL, status, data);
    assert.equal(h.synth.speaking, false, label + ": the closing cue is still playing");
    assert.equal(h.said.length, said, label);
    assert.deepEqual(P.releases(h), [G.g1], label);
  }
});

// The reported cases, as they were reproduced.
test("End with the send out, then a late 409 grant not current while the cue plays: cut and let go", async () => {
  const h = await sending(true);
  h.ctl.endTap();
  const said = playing(h, "late refusal");
  await h.answer(PATH.SEND, 409, { error: "conversation grant not current" });
  assert.equal(h.view().reason, REASON.END_TAPPED + "; " + L.withDetail(REASON.NOT_SENT, "conversation grant not current"));
  cut(h, said, "late refusal");
});

test("429 ticket table full on the ticket path speaks nothing", async () => {
  const h = await sending(false);
  await h.answer(PATH.TICKET, 429, { error: "ticket table full" });
  assert.equal(h.view().reason, L.withDetail(REASON.NOT_SENT, "ticket table full"));
  noCue(h, "ticket table full");
});

test("an unenrolled target at the unchanged generation: silent while listening, and cuts a cue playing", async () => {
  const unenrolled = () => snap({ targets: [target({ enrolled: false })] });
  const h = await listening(harness());
  h.ctl.snapshot(unenrolled());
  await flush();
  assert.equal(h.view().reason, REASON.TARGET);
  noCue(h, "listening");
  const g = await listening(harness());
  g.ctl.endTap();
  const said = playing(g, "cue");
  g.ctl.snapshot(unenrolled());
  await flush();
  cut(g, said, "cue");
});

test("busy in the wrong shape speaks nothing: 200 on the ticket and the send, 503 on the send", async () => {
  for (const [path, status] of [[PATH.TICKET, 200], [PATH.SEND, 200], [PATH.SEND, 503]]) {
    const h = await sending(path === PATH.SEND);
    await h.answer(path, status, { error: "busy" });
    assert.equal(h.phase(), PHASE.ENDED, path + " " + status);
    noCue(h, path + " " + status);
  }
});
