// What an end says is built from two facts, each settled once by its own
// evidence: the cause, which the body of the call whose status ended the
// conversation settles, and what became of a send POST the end left open.
// Settling one never touches the other, and a settled fact is final. Every
// call's body may settle the reason only until that call's own deadline, at
// which the fetch is aborted. Through the real page wiring (page_harness.js).
// Run by tests/test_desk_conversation_js.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { G } = require("./grant_ids.js");
const { L, PATH, REASON, settle, paired, snap, ended, voiceSend, listen, drafted } = require("./page_harness.js");
const { LIMITS } = L;

const failed = (status) => L.withDetail(REASON.CALL_FAILED, "HTTP " + status);
const NOT_CURRENT = "conversation grant not current";

// Time passing with the hub renewing every beat, up to `until` (h.t).
async function aliveUntil(h, until) {
  while (h.t < until) {
    await h.advance(Math.min(500, until - h.t));
    for (const call of h.pending(PATH.BEAT)) { call.answer(200, { grant: G.g1, expires_in: 15 }); await settle(); }
  }
}

// A voice send's POST out, then a later beat's 409 ending the conversation
// with that POST still open, its body held. The beat starts 3 s before the
// POST's deadline, so either can come first: the send's answer, its
// deadline, the beat's body or that body's loss.
async function beatEndsWhileSending() {
  const h = await paired();
  await listen(h);
  const granted = h.t;
  await h.advance(3000);
  await voiceSend(h, "check the logs");
  const send = h.pending(PATH.SEND)[0];
  const sendDeadline = h.t + LIMITS.SEND_DEADLINE_MS;
  await aliveUntil(h, granted + 4 * LIMITS.BEAT_MS - 1);
  await h.advance(1);
  const beat = await h.headers(PATH.BEAT, 409);
  assert.equal(h.state(), ended(failed(409) + "; " + REASON.UNKNOWN));
  return { h, send, beat, sendDeadline };
}

// The send's outcomes: what arrives, and the delivery it establishes.
const SEND = {
  "handed off": { act: async (s) => { s.send.answer(200, { request_id: "r1", state: "handed_off" }); await settle(); },
    shows: REASON.HANDED_OFF, definitive: true },
  "not sent": { act: async (s) => { s.send.answer(409, { error: NOT_CURRENT }); await settle(); },
    shows: L.withDetail(REASON.NOT_SENT, NOT_CURRENT), definitive: true },
  "its body lost": { act: async (s) => { await (await s.h.headers(s.send, 200)).lose(); },
    shows: REASON.UNKNOWN, definitive: false },
  "its deadline": { act: async (s) => { await s.h.advance(s.sendDeadline - s.h.t); },
    shows: REASON.UNKNOWN, definitive: false }
};

// The ending call's body: what arrives, and the cause it establishes.
const CAUSE = {
  "the beat's body": { act: (s) => s.beat.arrive({ error: NOT_CURRENT }), shows: REASON.GRANT_LOST },
  "the beat's body lost": { act: (s) => s.beat.lose(), shows: failed(409) }
};

test("cause and delivery settle apart, in every order, and stay settled", async () => {
  for (const [causeName, cause] of Object.entries(CAUSE)) {
    for (const [sendName, send] of Object.entries(SEND)) {
      for (const causeFirst of [true, false]) {
        const label = (causeFirst ? [causeName, sendName] : [sendName, causeName]).join(", then ");
        const s = await beatEndsWhileSending();
        const steps = causeFirst ? [["cause", cause], ["send", send]] : [["send", send], ["cause", cause]];
        let settledCause = false;
        let settledSend = false;
        for (const [kind, step] of steps) {
          await step.act(s);
          if (kind === "cause") settledCause = true; else settledSend = true;
          const shown = s.h.state();
          if (settledSend && send.definitive) assert.ok(!shown.includes(REASON.UNKNOWN), label + ": " + shown);
          if (!(settledSend && sendName === "not sent")) assert.ok(!shown.includes(REASON.NOT_SENT), label + ": " + shown);
          if (settledCause) assert.ok(shown.startsWith(L.CUE.ENDED + ": " + cause.shows + ";"), label + ": " + shown);
        }
        const final = ended(cause.shows + "; " + send.shows);
        assert.equal(s.h.state(), final, label);
        // Nothing later, weaker or not, changes a settled fact.
        await s.h.advance(LIMITS.SEND_DEADLINE_MS + LIMITS.STEP_MS);
        assert.equal(s.h.state(), final, label + ", later");
      }
    }
  }
});

// The reviewer's reproduction: the beat's refusal body, arriving after the
// send's definitive answer, refines the cause and leaves delivery alone.
for (const sendName of ["handed off", "not sent"]) {
  test(`a beat's late body after the send was ${sendName} leaves the send's outcome shown`, async () => {
    const s = await beatEndsWhileSending();
    await SEND[sendName].act(s);
    assert.equal(s.h.state(), ended(failed(409) + "; " + SEND[sendName].shows));
    await s.beat.arrive({ error: NOT_CURRENT });
    assert.equal(s.h.state(), ended(REASON.GRANT_LOST + "; " + SEND[sendName].shows));
  });
}

// A status arriving once freshness has lapsed, before the tick notices, is
// still a definite failure: it is recorded, and its body says more.
test("a ticket's 429 just past the freshness deadline shows its status, then its body's words", async () => {
  const h = await paired();
  await listen(h);
  const granted = h.t;
  await h.advance(LIMITS.FRESH_MS - 500);
  await drafted(h);
  assert.equal(h.pending(PATH.TICKET).length, 1);
  await h.advance(granted + LIMITS.FRESH_MS + 100 - h.t);
  assert.equal(h.state(), "Sending. The mic is closed.", "the tick already ended it");
  const body = await h.headers(PATH.TICKET, 429);
  assert.equal(h.state(), ended(failed(429)));
  await body.arrive({ error: "ticket table full" });
  assert.equal(h.state(), ended(L.withDetail(REASON.NOT_SENT, "ticket table full")));
});

// Each conversation call, its status ending the conversation, its body
// held: the body read is aborted at the call's own limit, counted from when
// the call was made, and the status stays shown.
const CALLS = {
  "the Start snapshot": {
    reach: async (h) => { h.$("conv-start").click(); await settle(); return h.pending(PATH.STATE)[0]; },
    status: 503, limit: LIMITS.STEP_MS, shows: failed(503)
  },
  "the grant request": {
    reach: async (h) => {
      h.$("conv-start").click();
      await settle();
      await h.answer(PATH.STATE, 200, snap());
      return h.pending(PATH.GRANT)[0];
    },
    status: 409, limit: LIMITS.STEP_MS, shows: failed(409)
  },
  "the beat": {
    reach: async (h) => { await listen(h); await h.advance(LIMITS.BEAT_MS); return h.pending(PATH.BEAT)[0]; },
    status: 409, limit: LIMITS.BEAT_ABORT_MS, shows: failed(409)
  },
  "the voice ticket": {
    reach: async (h) => { await listen(h); await drafted(h); return h.pending(PATH.TICKET)[0]; },
    status: 429, limit: LIMITS.STEP_MS, shows: failed(429)
  },
  "the voice send": {
    reach: async (h) => { await listen(h); await voiceSend(h, "x"); return h.pending(PATH.SEND)[0]; },
    status: 409, limit: LIMITS.SEND_DEADLINE_MS, shows: failed(409) + "; " + REASON.UNKNOWN
  }
};

for (const [name, c] of Object.entries(CALLS)) {
  test(`${name}: a body held after its ${c.status} is aborted at the call's own limit`, async () => {
    const h = await paired();
    const call = await c.reach(h);
    const made = h.t;
    await h.advance(100);
    const body = await h.headers(call, c.status);
    assert.equal(h.state(), ended(c.shows));
    await h.advance(made + c.limit - 1 - h.t);
    assert.ok(!call.aborted, "aborted before its limit");
    await h.advance(1);
    assert.ok(call.aborted, "not aborted at its limit");
    await body.arrive({ error: "busy" });
    assert.equal(h.state(), ended(c.shows));
  });
}

test("a ticket's 429 body held for a minute is never accepted", async () => {
  const h = await paired();
  await listen(h);
  await drafted(h);
  const body = await h.headers(PATH.TICKET, 429);
  await h.advance(60000);
  await body.arrive({ error: "ticket table full" });
  assert.equal(h.state(), ended(failed(429)));
});
