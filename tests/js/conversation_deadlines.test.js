// Every limit is an absolute time: an answer handled after a call's
// deadline, or after another limit, but before its timer's callback has run
// (a busy event loop), finds that limit already applied, as if the timer had
// fired on time. Each case lets the event loop stall for a moment just
// across a limit, so no fake timer runs, and only then delivers the answer.
// Through the real page wiring (page_harness.js). Run by
// tests/test_desk_conversation_js.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { G } = require("./grant_ids.js");
const {
  L, PATH, REASON, CUE, POLL, settle, paired, snap, voiceRq, ended, releases, voiceSend, listen, drafted
} = require("./page_harness.js");
const { LIMITS } = L;

const failed = (status) => L.withDetail(REASON.CALL_FAILED, "HTTP " + status);
const STALL_MS = 200;

// Time passing with the hub renewing every beat, up to `until` (h.t), and,
// with `shows`, answering each poll with the snapshot it makes.
async function aliveUntil(h, until, shows) {
  while (h.t < until) {
    await h.advance(Math.min(500, until - h.t));
    for (const b of h.pending(PATH.BEAT)) { b.answer(200, { grant: G.g1, expires_in: 15 }); await settle(); }
    if (shows) for (const p of h.pending(POLL)) { p.answer(200, shows()); await settle(); }
  }
}

// The event loop stalls across `limit`: the clock passes it, no timer runs.
function stallAcross(h, limit) {
  assert.ok(h.t < limit && h.t + STALL_MS > limit, "the stall does not cross the limit");
  h.t += STALL_MS;
}

const spoke = (h, text) => h.said.some((u) => u.text === text);

test("a valid hand-off handled just past the send POST's deadline ends with delivery unknown, no 'Sent.'", async () => {
  const h = await paired();
  await listen(h);
  await voiceSend(h, "check the logs");
  const deadline = h.t + LIMITS.SEND_DEADLINE_MS;
  await aliveUntil(h, deadline - 100);
  assert.equal(h.state(), "Sending. The mic is closed.");
  stallAcross(h, deadline);
  h.pending(PATH.SEND)[0].answer(200, { request_id: "r1", state: "handed_off" });
  await settle();
  assert.equal(h.state(), ended(REASON.UNKNOWN));
  assert.ok(!spoke(h, CUE.SENT), "'Sent.' was spoken");
  assert.deepEqual(releases(h), [G.g1]);
});

test("a ticket's 429 body handled just past the call's own limit leaves the status shown", async () => {
  const h = await paired();
  await listen(h);
  await drafted(h);
  const deadline = h.t + LIMITS.STEP_MS;
  const body = await h.headers(PATH.TICKET, 429);
  assert.equal(h.state(), ended(failed(429)));
  await h.advance(deadline - 100 - h.t);
  stallAcross(h, deadline);
  await body.arrive({ error: "ticket table full" });
  assert.equal(h.state(), ended(failed(429)));
});

test("the Start snapshot handled just past the step limit fails the start; no grant is asked for", async () => {
  const h = await paired();
  h.$("conv-start").click();
  await settle();
  const deadline = h.t + LIMITS.STEP_MS;
  h.t = deadline - 100;
  stallAcross(h, deadline);
  await h.answer(PATH.STATE, 200, snap());
  assert.equal(h.state(), ended(REASON.HUB_UNREACHABLE));
  assert.equal(h.all(PATH.GRANT).length, 0, "a grant was asked for");
});

test("a wake lock granted just past the step limit fails the start and is let go", async () => {
  const h = await paired();
  const wake = h.win.navigator.wakeLock;
  const request = wake.request;
  let grantLock;
  wake.request = () => new Promise((resolve) => { grantLock = () => resolve(request()); });
  h.$("conv-start").click();
  await settle();
  await h.answer(PATH.STATE, 200, snap());
  assert.ok(grantLock, "no wake lock was asked for");
  const deadline = h.t + LIMITS.STEP_MS;
  h.t = deadline - 100;
  stallAcross(h, deadline);
  grantLock();
  await settle();
  assert.equal(h.state(), ended(REASON.NO_WAKE));
  assert.equal(h.sentinels[0].released, 1, "the late wake lock was kept");
  assert.equal(h.all(PATH.GRANT).length, 0, "a grant was asked for");
});

test("a grant handled just past the step limit fails the start, and that grant is released", async () => {
  const h = await paired();
  h.$("conv-start").click();
  await settle();
  await h.answer(PATH.STATE, 200, snap());
  const deadline = h.t + LIMITS.STEP_MS;
  h.t = deadline - 100;
  stallAcross(h, deadline);
  await h.answer(PATH.GRANT, 200, { grant: G.g1, expires_in: 15 });
  assert.equal(h.state(), ended(REASON.HUB_UNREACHABLE));
  assert.equal(h.rec(), undefined, "a mic was opened");
  assert.deepEqual(releases(h), [G.g1]);
});

test("a beat's renewal handled just past its abort limit renews nothing and ends the conversation silently", async () => {
  const h = await paired();
  await listen(h);
  await h.advance(LIMITS.BEAT_MS);
  const said = h.said.length;
  const deadline = h.t + LIMITS.BEAT_ABORT_MS;
  h.t = deadline - 100;
  stallAcross(h, deadline);
  await h.answer(PATH.BEAT, 200, { grant: G.g1, expires_in: 15 });
  assert.match(h.state(), /^Conversation ended: /);
  assert.equal(h.said.length, said, "the page spoke");
  assert.deepEqual(releases(h), [G.g1]);
});

test("a voice ticket handled just past the step limit posts nothing", async () => {
  const h = await paired();
  await listen(h);
  await drafted(h);
  const deadline = h.t + LIMITS.STEP_MS;
  await aliveUntil(h, deadline - 100);
  stallAcross(h, deadline);
  await h.answer(PATH.TICKET, 200, { ticket: "t1" });
  assert.equal(h.state(), ended(L.withDetail(REASON.NOT_SENT, REASON.HUB_UNREACHABLE)));
  assert.equal(h.all(PATH.SEND).length, 0, "the prompt was posted");
});

// A voice send handed off, its "Sent." cue over: waiting for the request.
async function waiting(h) {
  await listen(h);
  await voiceSend(h, "check the logs");
  await h.answer(PATH.SEND, 200, { request_id: "r1", state: "handed_off" });
  assert.ok(spoke(h, CUE.SENT));
  h.utt().fire("end");
  await settle();
  return h.t;
}

test("the request first seen in a snapshot handled just past the 30 s wait ends as unknown", async () => {
  const h = await paired();
  const deadline = await waiting(h) + LIMITS.ACK_WAIT_MS;
  await aliveUntil(h, deadline - 100);
  assert.equal(h.state(), "Sent. Waiting for the reply. The mic is closed.");
  stallAcross(h, deadline);
  await h.answer(POLL, 200, snap({ requests: [voiceRq("r1", "handed_off")] }));
  assert.equal(h.state(), ended(REASON.UNKNOWN));
});

test("a reply in a snapshot handled just past the ten-minute wait is never spoken", async () => {
  const h = await paired();
  const deadline = await waiting(h) + LIMITS.REPLY_WAIT_MS;
  await aliveUntil(h, deadline - 100, () => snap({ requests: [voiceRq("r1", "handed_off")] }));
  assert.equal(h.state(), "Sent. Waiting for the reply. The mic is closed.");
  stallAcross(h, deadline);
  await h.answer(POLL, 200, snap({ requests: [voiceRq("r1", "answered", "the late reply")] }));
  assert.ok(!spoke(h, "the late reply"), "the reply was spoken past the wait");
  assert.equal(h.state(), ended(REASON.REPLY_TIMEOUT));
});

// Answers handled just before the same limits are used: the limit is the
// time, not a margin.
test("a hand-off handled just before the send POST's deadline is used", async () => {
  const h = await paired();
  await listen(h);
  await voiceSend(h, "check the logs");
  await aliveUntil(h, h.t + LIMITS.SEND_DEADLINE_MS - STALL_MS);
  h.t += STALL_MS - 1;
  await h.answer(PATH.SEND, 200, { request_id: "r1", state: "handed_off" });
  assert.ok(spoke(h, CUE.SENT));
  assert.equal(h.state(), "Sent. Waiting for the reply. The mic is closed.");
});
