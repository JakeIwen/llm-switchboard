// The gate, in the page harness (page_harness.js, the real app.js wiring):
// any status but 200 from the hub, on any call the page makes, cuts a
// closing cue and ends a running conversation silently at its status line,
// before any body; what the end shows settles once the call's own body
// arrives. Run by tests/test_desk_conversation_js.py with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { G } = require("./grant_ids.js");
const {
  STATIC, L, PATH, REASON, POLL, settle, paired, snap, ended, releases, voiceSend, listen, cue, drafted, tapRefresh
} = require("./page_harness.js");
const { CALLS } = require("./page_calls.js");
const { LIMITS } = L;

const failed = (status) => L.withDetail(REASON.CALL_FAILED, "HTTP " + status);

const STATUSES = [409, 503];

for (const [name, modes] of Object.entries(CALLS)) {
  for (const status of STATUSES) {
    if (modes.closing) {
      test(`${name}: a ${status} at its status line cuts a closing cue before any body`, async () => {
        const h = await paired();
        const call = await modes.closing(h);
        assert.ok(call && !call.done, "no open call");
        const said = h.said.length;
        const cueUtt = h.utt();
        const released = releases(h).length;
        await h.headers(call, status);
        assert.equal(h.synth.speaking, false, "the closing cue still plays");
        assert.ok(cueUtt.over, "the closing cue was not cut");
        assert.equal(h.said.length, said, "the page spoke");
        assert.equal(releases(h).length, released + 1, "the cue's grant was not released");
        assert.equal(h.$("conv-start").disabled, true, "Start is offered while the cut cue may still sound");
      });
    }
    test(`${name}: a ${status} at its status line ends a running conversation silently before any body`, async () => {
      const h = await paired();
      const call = await modes.running(h);
      assert.ok(call && !call.done, "no open call");
      const said = h.said.length;
      const rec = h.rec();
      const listening = h.state() === "Listening.";
      const released = releases(h).length;
      await h.headers(call, status);
      assert.match(h.state(), /^Conversation ended: /);
      assert.equal(h.said.length, said, "the page spoke");
      if (listening) assert.ok(rec.aborts > 0, "the recognizer was not stopped");
      if (!modes.starting) assert.equal(releases(h).length, released + 1, "the grant was not released");
    });
  }
}

test("only hub_api.js reaches the network, and only through fetch", () => {
  const files = fs.readdirSync(STATIC).filter((f) => f.endsWith(".js"));
  const network = /\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|importScripts/g;
  const uses = Object.fromEntries(files.map((f) => [f, (fs.readFileSync(path.join(STATIC, f), "utf8").match(network) || [])]));
  for (const f of files) {
    if (f === "hub_api.js") assert.deepEqual(uses[f].map((u) => u.replace(/\s/g, "")), ["fetch("]);
    else assert.deepEqual(uses[f], [], f + " reaches the network itself");
  }
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  assert.ok(html.indexOf('src="/desk/hub_api.js"') < html.indexOf('src="/desk/app.js"'));
});

// What the end shows at the status line, and once the call's own body
// arrives: what that call would have shown.
const SETTLES = [
  { name: "a voice send's 409", reach: async (h) => { await listen(h); await voiceSend(h, "x"); return h.pending(PATH.SEND)[0]; },
    status: 409, body: { error: "busy" }, provisional: failed(409) + "; " + REASON.UNKNOWN,
    settled: L.withDetail(REASON.NOT_SENT, "busy") },
  { name: "a voice ticket's 429", reach: async (h) => { await listen(h); await drafted(h); return h.pending(PATH.TICKET)[0]; },
    status: 429, body: { error: "ticket table full" }, provisional: failed(429),
    settled: L.withDetail(REASON.NOT_SENT, "ticket table full") },
  { name: "a beat's 409", reach: async (h) => { await listen(h); await h.advance(LIMITS.BEAT_MS); return h.pending(PATH.BEAT)[0]; },
    status: 409, body: { error: "conversation grant not current" }, provisional: failed(409), settled: REASON.GRANT_LOST },
  { name: "a poll's 503", reach: async (h) => { await listen(h); return h.pending(POLL)[0]; },
    status: 503, body: { error: "lock store error" }, provisional: failed(503), settled: failed(503) },
  { name: "a voice send's 401", reach: async (h) => { await listen(h); await voiceSend(h, "x"); return h.pending(PATH.SEND)[0]; },
    status: 401, body: { error: "unauthorized" },
    provisional: REASON.UNPAIRED + "; " + L.withDetail(REASON.NOT_SENT, "HTTP 401"),
    settled: REASON.UNPAIRED + "; " + L.withDetail(REASON.NOT_SENT, "unauthorized") },
  { name: "a grant request's 409", reach: CALLS["the grant request"].running,
    status: 409, body: { error: "busy" }, provisional: failed(409), settled: L.withDetail(REASON.REFUSED, "busy") },
  { name: "a Start snapshot's 503", reach: CALLS["the Start snapshot"].running,
    status: 503, body: { error: "unavailable" }, provisional: failed(503), settled: REASON.HUB_UNREACHABLE }
];

for (const c of SETTLES) {
  test(`${c.name}: the end shows its status, then what its body says`, async () => {
    const h = await paired();
    const body = await h.headers(await c.reach(h), c.status);
    assert.equal(h.state(), ended(c.provisional));
    await body.arrive(c.body);
    assert.equal(h.state(), ended(c.settled));
  });
}

test("a voice send's 409 whose body is lost keeps its status and never claims 'not sent'", async () => {
  const h = await paired();
  await listen(h);
  await voiceSend(h, "x");
  const body = await h.headers(PATH.SEND, 409);
  await body.lose();
  await h.advance(LIMITS.SEND_DEADLINE_MS);
  assert.equal(h.state(), ended(failed(409) + "; " + REASON.UNKNOWN));
  assert.ok(!h.state().includes(REASON.NOT_SENT));
});

// Each reproduction below as the reviewer ran it.

test("a beat's 409 ends the open mic at its headers, past its abort and a lost body; 'stop listening' then says nothing", async () => {
  const h = await paired();
  await listen(h);
  await h.advance(LIMITS.BEAT_MS);
  const rec = h.rec();
  const said = h.said.length;
  const body = await h.headers(PATH.BEAT, 409);
  await h.advance(LIMITS.BEAT_ABORT_MS);
  await body.lose();
  assert.notEqual(h.state(), "Listening.");
  assert.ok(rec.aborts > 0, "the recognizer still listens");
  assert.deepEqual(releases(h), [G.g1]);
  rec.hear("stop listening");
  await settle();
  assert.equal(h.said.length, said, "a closing cue started");
});

test("a send's 409 after End cuts the closing cue at its headers, its body held", async () => {
  const h = await paired();
  await listen(h);
  await voiceSend(h, "check the logs");
  await cue(h);
  const said = h.said.length;
  await h.headers(PATH.SEND, 409);
  assert.equal(h.synth.speaking, false, "the closing cue still plays");
  assert.equal(h.said.length, said);
  assert.deepEqual(releases(h), [G.g1]);
});

test("a voice send's 401 stays 'not sent' past the POST's deadline and a lost body", async () => {
  const h = await paired();
  await listen(h);
  await voiceSend(h, "check the logs");
  const body = await h.headers(PATH.SEND, 401);
  await h.advance(LIMITS.SEND_DEADLINE_MS);
  await body.lose();
  assert.equal(h.state(), ended(REASON.UNPAIRED + "; " + L.withDetail(REASON.NOT_SENT, "HTTP 401")));
});

// The four answers the closing cue's check never saw: how to reach each
// with the cue playing, and the answer.
const UNSEEN = {
  "the tap flow's refresh showing sends off": [
    async (h) => { const r = await tapRefresh(h); await listen(h); await cue(h); return r; },
    200, snap({ sends_enabled: false })],
  "a failed abandoned Start snapshot": [CALLS["an abandoned Start snapshot"].closing, 503, { error: "unavailable" }],
  "an earlier grant release's 409": [CALLS["an earlier conversation's grant release"].closing, 409,
    { error: "conversation grant not current" }],
  "the startup listing's 503": [CALLS["the startup listing"].closing, 503, { error: "unavailable" }]
};

for (const [name, [reach, status, data]] of Object.entries(UNSEEN)) {
  test(`${name} cuts a closing cue and releases its grant`, async () => {
    const h = await paired();
    const call = await reach(h);
    const said = h.said.length;
    const released = releases(h).length;
    call.answer(status, data);
    await settle();
    assert.equal(h.synth.speaking, false, "the closing cue still plays");
    assert.equal(h.said.length, said);
    assert.equal(releases(h).length, released + 1, "the cue's grant was not released");
  });
}
