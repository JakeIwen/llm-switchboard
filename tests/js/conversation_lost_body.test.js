// A 200 whose body never arrives, on any call the page makes: hub_api.js
// reports it at the shared HTTP boundary, before the caller's own code, and
// the controller's one entry for it (bodyLost) silences, as the gate does
// for any other status. A body that arrived empty or not JSON is content,
// left to each call's own checks. Through the real page wiring
// (page_harness.js). Run by tests/test_desk_conversation_js.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { G } = require("./grant_ids.js");
const {
  L, PATH, REASON, POLL, settle, paired, snap, ended, releases, voiceSend, listen, cue, over, tapTicket
} = require("./page_harness.js");
const { CALLS } = require("./page_calls.js");
const { LIMITS } = L;

for (const [name, modes] of Object.entries(CALLS)) {
  if (modes.closing) {
    test(`${name}: a 200 whose body is lost cuts a closing cue`, async () => {
      const h = await paired();
      const call = await modes.closing(h);
      assert.ok(call && !call.done, "no open call");
      const said = h.said.length;
      const cueUtt = h.utt();
      const released = releases(h).length;
      const body = await h.headers(call, 200);
      assert.equal(h.synth.speaking, true, "a 200's status line alone cut the cue");
      await body.lose();
      assert.equal(h.synth.speaking, false, "the closing cue still plays");
      assert.ok(cueUtt.over, "the closing cue was not cut");
      assert.equal(h.said.length, said, "the page spoke");
      assert.equal(releases(h).length, released + 1, "the cue's grant was not released");
    });
  }
  test(`${name}: a 200 whose body is lost ends a running conversation silently`, async () => {
    const h = await paired();
    const call = await modes.running(h);
    assert.ok(call && !call.done, "no open call");
    const said = h.said.length;
    const rec = h.rec();
    const listening = h.state() === "Listening.";
    const released = releases(h).length;
    const body = await h.headers(call, 200);
    if (listening) assert.equal(h.state(), "Listening.", "a 200's status line alone ended it");
    await body.lose();
    assert.match(h.state(), /^Conversation ended: /);
    assert.equal(h.said.length, said, "the page spoke");
    assert.equal(h.synth.speaking, false);
    if (listening) {
      assert.ok(rec.aborts > 0, "the recognizer was not stopped");
      rec.hear("stop listening");
      await settle();
      assert.equal(h.said.length, said, "'stop listening' started a cue");
    }
    if (!modes.starting) assert.equal(releases(h).length, released + 1, "the grant was not released");
  });
}

// Each reproduction as the reviewer ran it.

test("the startup listing's lost 200 body stops the open mic and cuts a closing cue", async () => {
  const h = await paired();
  await listen(h);
  const rec = h.rec();
  await (await h.headers("/desk/api/startup", 200)).lose();
  assert.ok(rec.aborts > 0, "the recognizer still listens");
  assert.deepEqual(releases(h), [G.g1]);
  const c = await paired();
  await listen(c);
  await cue(c);
  await (await c.headers("/desk/api/startup", 200)).lose();
  assert.equal(c.synth.speaking, false, "the closing cue still plays");
  assert.deepEqual(releases(c), [G.g1]);
});

test("an abandoned tap ticket's lost 200 body ends the conversation; 'stop listening' then says nothing", async () => {
  const h = await paired();
  tapTicket(h);
  await listen(h);
  const rec = h.rec();
  const said = h.said.length;
  await (await h.headers("/desk/api/ticket", 200)).lose();
  assert.notEqual(h.state(), "Listening.");
  rec.hear("stop listening");
  await settle();
  assert.equal(h.said.length, said, "a closing cue started");
});

test("an earlier grant release's lost 200 body cuts a newer closing cue", async () => {
  const h = await paired();
  await listen(h);
  await over(h);
  await listen(h, G.g2);
  await cue(h);
  await (await h.headers(PATH.RELEASE, 200)).lose();
  assert.equal(h.synth.speaking, false, "the closing cue still plays");
  assert.deepEqual(releases(h), [G.g1, G.g2]);
});

test("an earlier pairing's startup listing whose 200 body is lost ends the new pairing's conversation", async () => {
  const h = await paired();
  const old = h.pending("/desk/api/startup")[0];
  await h.answer(POLL, 401, { error: "unauthorized" });
  await h.advance(2000);
  assert.ok(h.$("pairing").hidden, "the page did not pair again");
  await h.answer(PATH.STATE, 200, snap());
  await listen(h);
  const rec = h.rec();
  await (await h.headers(old, 200)).lose();
  assert.equal(h.state(), ended(REASON.UNUSABLE));
  assert.ok(rec.aborts > 0, "the recognizer still listens");
  assert.deepEqual(releases(h), [G.g1]);
});

// The send's own answer still settles what became of the prompt.
test("a voice send whose 200 body is lost ends silently with delivery unknown, never 'not sent' or 'handed off'", async () => {
  const h = await paired();
  await listen(h);
  await voiceSend(h, "check the logs");
  const said = h.said.length;
  await (await h.headers(PATH.SEND, 200)).lose();
  await h.advance(LIMITS.SEND_DEADLINE_MS);
  assert.equal(h.state(), ended(REASON.UNUSABLE + "; " + REASON.UNKNOWN));
  assert.ok(!h.state().includes(REASON.NOT_SENT));
  assert.ok(!h.state().includes(REASON.HANDED_OFF));
  assert.equal(h.said.length, said, "the page spoke");
  assert.deepEqual(releases(h), [G.g1]);
});

// A body that arrived is content: an empty or non-JSON one is not a lost
// body, and a call that ignores it (the startup listing) leaves the
// conversation and its cue alone.
test("a 200 whose body arrived empty or not JSON is not a lost body", async () => {
  for (const text of ["", "not json"]) {
    const h = await paired();
    await listen(h);
    await (await h.headers("/desk/api/startup", 200)).raw(text);
    assert.equal(h.state(), "Listening.", JSON.stringify(text));
    const c = await paired();
    await listen(c);
    await cue(c);
    await (await c.headers("/desk/api/startup", 200)).raw(text);
    assert.equal(c.synth.speaking, true, JSON.stringify(text) + ": the closing cue was cut");
    assert.deepEqual(releases(c), [], JSON.stringify(text));
  }
});

// Time passing with the hub renewing every beat.
async function alive(h, ms) {
  const until = h.t + ms;
  while (h.t < until) {
    await h.advance(Math.min(500, until - h.t));
    for (const b of h.pending(PATH.BEAT)) { b.answer(200, { grant: G.g1, expires_in: 15 }); await settle(); }
  }
}

// The page's own aborts. At a call's own limit, a read it aborts after the
// status line is no body received, and silences; before any status line
// it is no answer, which ends nothing by itself.
test("the poll aborted at its limit after a 200's status line ends the conversation; before one, it ends nothing", async () => {
  const h = await paired();
  await listen(h);
  await h.headers(POLL, 200);
  await alive(h, 35000);
  assert.equal(h.state(), ended(REASON.UNUSABLE));
  const k = await paired();
  await listen(k);
  await alive(k, 35000);
  assert.ok(k.calls.some((c) => c.path.startsWith(POLL) && c.aborted), "the poll was not aborted at its limit");
  assert.equal(k.state(), "Listening.");
});

// The page tidying up after its own end: the beat it aborts when the cue
// ends silences nothing that is still running, and the next conversation
// starts and listens.
test("a beat's read aborted when the closing cue ends leaves the next conversation alone", async () => {
  const h = await paired();
  await listen(h);
  await cue(h);
  await h.advance(LIMITS.BEAT_MS);
  const beat = h.pending(PATH.BEAT)[0];
  await h.headers(beat, 200);
  h.utt().fire("end");
  await settle();
  assert.ok(beat.aborted, "the beat was not aborted with the cue");
  await h.advance(LIMITS.GUARD_MS);
  await listen(h, G.g2);
  assert.equal(h.state(), "Listening.");
});
