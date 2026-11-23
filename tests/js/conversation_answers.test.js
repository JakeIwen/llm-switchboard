// The controller's closing cue after the hub answered a turn's ticket or
// send with something other than success: only a refusal that keeps
// ownership speaks; an answer the page can't use is as unknown as a
// refusal it has never seen. Run by tests/test_desk_conversation_js.py
// with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../../llm_switchboard/desk/static/conversation_logic.js");
const C = require("../../llm_switchboard/desk/static/conversation.js");
const { REASON, CUE, LIMITS } = L;
const { PATH } = C;
const { flush, harness, listening, texts, releases } = require("./controller_harness.js");

const cue = (reason) => [CUE.ENDED + ": " + reason + "."];

// Through "send it"; with `posted`, the ticket is answered and the send POST is out.
async function turn(posted) {
  const h = await listening(harness());
  await h.hear("check the logs");
  await h.hear("send it");
  if (posted) await h.answer(PATH.TICKET, 200, { ticket: "t" });
  return h;
}

test("a 200 ticket answer without a ticket ends the conversation as lost, with no cue", async () => {
  for (const data of [{}, null, { ticket: 7 }]) {
    const h = await turn(false);
    await h.answer(PATH.TICKET, 200, data);
    assert.equal(h.view().reason, L.withDetail(REASON.NOT_SENT, "HTTP 200"), JSON.stringify(data));
    assert.equal(h.view().lost, true, JSON.stringify(data));
    assert.equal(h.said.length, 0, JSON.stringify(data));
    assert.deepEqual(releases(h), ["g1"], JSON.stringify(data));
  }
});

test("a 200 send answer the page can't use ends the conversation as lost, with no cue", async () => {
  for (const data of [{}, null, { request_id: "r1", state: "queued" }, { state: "handed_off" }]) {
    const h = await turn(true);
    await h.answer(PATH.SEND, 200, data);
    assert.equal(h.view().reason, REASON.UNKNOWN, JSON.stringify(data));
    assert.equal(h.view().lost, true, JSON.stringify(data));
    assert.equal(h.said.length, 0, JSON.stringify(data));
    assert.deepEqual(releases(h), ["g1"], JSON.stringify(data));
  }
});

test("a refusal that keeps ownership still speaks, and no answer at all is left to freshness", async () => {
  for (const posted of [false, true]) {
    const k = await turn(posted);
    await k.answer(posted ? PATH.SEND : PATH.TICKET, 409, { error: "busy" });
    assert.equal(k.view().lost, false);
    assert.deepEqual(texts(k), cue(L.withDetail(REASON.NOT_SENT, "busy")));
  }
  const t = await turn(false);
  t.pending(PATH.TICKET)[0].fail();
  await flush();
  assert.equal(t.view().lost, false);
  assert.deepEqual(texts(t), cue(L.withDetail(REASON.NOT_SENT, "the hub could not be reached")));
  const s = await turn(true);
  s.pending(PATH.SEND)[0].fail();
  await flush();
  assert.equal(s.view().lost, false);
  assert.deepEqual(texts(s), cue(REASON.UNKNOWN));
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
