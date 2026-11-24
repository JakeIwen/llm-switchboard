// What the controller makes of the hub's answers to a conversation's
// calls when they are not their call's success, or never come: the
// conversation ends silently and the grant is let go, an answer the page
// can't use included; a send's answer still settles what the screen says.
// Run by tests/test_desk_conversation_js.py with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { G } = require("./grant_ids.js");
const L = require("../../llm_switchboard/desk/static/conversation_logic.js");
const C = require("../../llm_switchboard/desk/static/conversation.js");
const { REASON, LIMITS } = L;
const { PATH } = C;
const { DEST, flush, snap, harness, listening, releases } = require("./controller_harness.js");

// Through "send it"; with `posted`, the ticket is answered and the send POST is out.
async function turn(posted) {
  const h = await listening(harness());
  await h.hear("check the logs");
  await h.hear("send it");
  if (posted) await h.answer(PATH.TICKET, 200, { ticket: "t" });
  return h;
}

test("a 200 ticket answer without a ticket ends the conversation silently", async () => {
  for (const data of [{}, null, { ticket: 7 }]) {
    const h = await turn(false);
    await h.answer(PATH.TICKET, 200, data);
    assert.equal(h.view().reason, L.withDetail(REASON.NOT_SENT, "HTTP 200"), JSON.stringify(data));
    assert.equal(h.said.length, 0, JSON.stringify(data));
    assert.deepEqual(releases(h), [G.g1], JSON.stringify(data));
  }
});

test("a 200 send answer the page can't use ends the conversation silently", async () => {
  for (const data of [{}, null, { request_id: "r1", state: "queued" }, { state: "handed_off" }]) {
    const h = await turn(true);
    await h.answer(PATH.SEND, 200, data);
    assert.equal(h.view().reason, REASON.UNKNOWN, JSON.stringify(data));
    assert.equal(h.said.length, 0, JSON.stringify(data));
    assert.deepEqual(releases(h), [G.g1], JSON.stringify(data));
  }
});

test("a ticket or send that gets no answer ends the conversation silently, the grant let go at once", async () => {
  const silent = (h, reason, what) => {
    assert.equal(h.view().reason, reason, what);
    assert.equal(h.said.length, 0, what);
    assert.equal(h.synth.speaking, false, what);
    assert.deepEqual(releases(h), [G.g1], what);
  };
  const t = await turn(false);
  t.pending(PATH.TICKET)[0].fail();
  await flush();
  silent(t, L.withDetail(REASON.NOT_SENT, "the hub could not be reached"), "ticket failed");
  const tt = await turn(false);
  await tt.alive(LIMITS.STEP_MS);
  silent(tt, L.withDetail(REASON.NOT_SENT, "the hub could not be reached"), "ticket timed out");
  const s = await turn(true);
  s.pending(PATH.SEND)[0].fail();
  await flush();
  silent(s, REASON.UNKNOWN, "send failed");
  const sd = await turn(true);
  await sd.alive(LIMITS.SEND_DEADLINE_MS);
  silent(sd, REASON.UNKNOWN, "send reached its deadline");
});

test("an unusable 200 for a send an end left open settles nothing and speaks nothing", async () => {
  const unknown = (reason) => reason + "; " + REASON.UNKNOWN;
  const e = await turn(true);
  e.ctl.endTap();
  const said = e.said.length;
  await e.answer(PATH.SEND, 200, {});
  assert.equal(e.view().reason, unknown(REASON.END_TAPPED));
  assert.equal(e.said.length, said);
  const f = await turn(true);
  await f.advance(LIMITS.FRESH_MS + LIMITS.TICK_MS);
  await f.answer(PATH.SEND, 200, { request_id: "r1" });
  assert.equal(f.view().reason, unknown(REASON.HUB_LOST));
  assert.equal(f.said.length, 0);
});

test("a send POST keeps its own deadline after the conversation ends; a newer conversation is untouched", async () => {
  const h = await turn(true);
  const post = h.pending(PATH.SEND)[0];
  const deadline = h.t + LIMITS.SEND_DEADLINE_MS;
  await h.advance(LIMITS.FRESH_MS + LIMITS.TICK_MS);
  assert.equal(h.view().active, false);
  assert.equal(h.view().reason, REASON.HUB_LOST + "; " + REASON.UNKNOWN);
  await listening(h);
  await h.alive(deadline - h.t - 1);
  assert.equal(post.aborted, undefined, "aborted before its deadline");
  await h.alive(1);
  assert.equal(post.aborted, true);
  assert.equal(h.phase(), L.PHASE.LISTENING);
  assert.equal(h.view().reason, null);
  assert.equal(h.view().serial, 2);
});

test("an ended conversation's send POST is aborted at its deadline and delivery stays unknown", async () => {
  const h = await turn(true);
  const post = h.pending(PATH.SEND)[0];
  h.ctl.endTap();
  await h.finish();
  await h.advance(LIMITS.SEND_DEADLINE_MS);
  assert.equal(post.aborted, true);
  assert.equal(h.view().reason, REASON.END_TAPPED + "; " + REASON.UNKNOWN);
  assert.equal(h.said.length, 1);
});

test("an acquisition's 200 without a grant in the hub's shape fails silently and never opens the mic", async () => {
  for (const data of [{ grant: " " }, { grant: G.g1.toUpperCase().replace(/1/g, "A") }, { grant: G.g1 + "0" },
    { grant: 7 }, {}, null]) {
    const h = harness();
    assert.equal(h.ctl.start(DEST), true);
    await flush();
    await h.answer(PATH.STATE, 200, snap());
    await h.answer(PATH.GRANT, 200, data);
    await h.advance(LIMITS.GUARD_MS);
    assert.equal(h.phase(), L.PHASE.ENDED, JSON.stringify(data));
    assert.equal(h.view().reason, L.withDetail(REASON.REFUSED, "HTTP 200"), JSON.stringify(data));
    assert.equal(h.recs.length, 0, JSON.stringify(data));
    assert.equal(h.said.length, 0, JSON.stringify(data));
  }
});

test("a beat's 200 that doesn't name the grant beaten renews nothing: the conversation ends silently", async () => {
  for (const data of [null, {}, { grant: G.g2, expires_in: 15 }]) {
    const h = await listening(harness());
    await h.advance(LIMITS.BEAT_MS);
    await h.answer(PATH.BEAT, 200, data);
    assert.equal(h.phase(), L.PHASE.ENDED, JSON.stringify(data));
    assert.equal(h.view().reason, REASON.GRANT_LOST, JSON.stringify(data));
    assert.equal(h.said.length, 0, JSON.stringify(data));
    assert.deepEqual(releases(h), [G.g1], JSON.stringify(data));
  }
  // The hub's real renewal still renews.
  const h = await listening(harness());
  await h.alive(LIMITS.FRESH_MS + 200);
  assert.equal(h.phase(), L.PHASE.LISTENING);
});
