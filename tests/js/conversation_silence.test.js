// An answer that silences the page does so whether a conversation is
// running or its closing cue is playing, whichever fetch or pairing brought
// it: every snapshot the page receives goes through the controller's one
// entry, and every 200 that is not its call's success (its body lost
// among them) through its one silencing function. Through the real page
// wiring (page_harness.js). Run by tests/test_desk_conversation_js.py.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { G } = require("./grant_ids.js");
const {
  L, PATH, REASON, POLL, settle, paired, snap, ended, releases, voiceSend, listen, cue, over, drafted,
  abandonedSnapshot, abandonedGrant, tapRefresh, repaired
} = require("./page_harness.js");
const { LIMITS } = L;

const gone = () => snap({ targets: [] });

// `data` may be a function, called at delivery, so a snapshot made for it is the newest yet.
const answer = (status, data) => async (h, call) => {
  call.answer(status, typeof data === "function" ? data() : data);
  await settle();
};
const lost = async (h, call) => { await (await h.headers(call, 200)).lose(); };

// Every kind of answer that cuts a closing cue: how to have its call open
// with a conversation listening (`running`, which returns the call and the
// grant that conversation holds) and with a closing cue playing
// (`closing`), and how the answer arrives.
const ANSWERS = {
  "the poll's snapshot showing the target gone": {
    running: async (h) => { await listen(h); return [h.pending(POLL)[0], G.g1]; },
    closing: async (h) => { await listen(h); await cue(h); return [h.pending(POLL)[0], G.g1]; },
    deliver: answer(200, gone)
  },
  "the tap flow's refresh showing the target gone": {
    running: async (h) => { const r = await tapRefresh(h); await listen(h); return [r, G.g1]; },
    closing: async (h) => { const r = await tapRefresh(h); await listen(h); await cue(h); return [r, G.g1]; },
    deliver: answer(200, gone)
  },
  "an abandoned Start snapshot showing the target gone": {
    running: async (h) => { const s = await abandonedSnapshot(h); await listen(h); return [s, G.g1]; },
    closing: async (h) => { const s = await abandonedSnapshot(h); await listen(h); await cue(h); return [s, G.g1]; },
    deliver: answer(200, gone)
  },
  "an earlier pairing's poll showing the target gone": {
    running: async (h) => { const p = await repaired(h); await listen(h); return [p, G.g1]; },
    closing: async (h) => { const p = await repaired(h); await listen(h); await cue(h); return [p, G.g1]; },
    deliver: answer(200, gone)
  },
  "the poll's lost body": {
    running: async (h) => { await listen(h); return [h.pending(POLL)[0], G.g1]; },
    closing: async (h) => { await listen(h); await cue(h); return [h.pending(POLL)[0], G.g1]; },
    deliver: lost
  },
  "the tap flow's refresh's lost body": {
    running: async (h) => { const r = await tapRefresh(h); await listen(h); return [r, G.g1]; },
    closing: async (h) => { const r = await tapRefresh(h); await listen(h); await cue(h); return [r, G.g1]; },
    deliver: lost
  },
  "an abandoned Start snapshot's lost body": {
    running: async (h) => { const s = await abandonedSnapshot(h); await listen(h); return [s, G.g1]; },
    closing: async (h) => { const s = await abandonedSnapshot(h); await listen(h); await cue(h); return [s, G.g1]; },
    deliver: lost
  },
  "an earlier pairing's poll's lost body": {
    running: async (h) => { const p = await repaired(h); await listen(h); return [p, G.g1]; },
    closing: async (h) => { const p = await repaired(h); await listen(h); await cue(h); return [p, G.g1]; },
    deliver: lost
  },
  "the beat's 200 that renews nothing": {
    running: async (h) => { await listen(h); await h.advance(LIMITS.BEAT_MS); return [h.pending(PATH.BEAT)[0], G.g1]; },
    closing: async (h) => {
      await listen(h);
      await cue(h);
      await h.advance(LIMITS.BEAT_MS);
      return [h.pending(PATH.BEAT)[0], G.g1];
    },
    deliver: answer(200, {})
  },
  "the beat's 200 whose body its abort lost": {
    running: async (h) => { await listen(h); await h.advance(LIMITS.BEAT_MS); return [h.pending(PATH.BEAT)[0], G.g1]; },
    closing: async (h) => {
      await listen(h);
      await cue(h);
      await h.advance(LIMITS.BEAT_MS);
      return [h.pending(PATH.BEAT)[0], G.g1];
    },
    deliver: async (h, call) => { await h.headers(call, 200); await h.advance(LIMITS.BEAT_ABORT_MS); }
  },
  "a beat given up on, then its 200 that renews nothing": {
    running: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await h.advance(LIMITS.BEAT_MS + LIMITS.BEAT_ABORT_MS);
      return [h.pending(PATH.BEAT)[0], G.g1];
    },
    closing: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await h.advance(LIMITS.BEAT_MS + LIMITS.BEAT_ABORT_MS - 500);
      await cue(h);
      await h.advance(500);
      return [h.pending(PATH.BEAT)[0], G.g1];
    },
    deliver: answer(200, {})
  },
  "an abandoned voice ticket's 200 with no ticket": {
    running: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await drafted(h);
      await over(h);
      await listen(h, G.g2);
      return [h.pending(PATH.TICKET)[0], G.g2];
    },
    closing: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await drafted(h);
      await cue(h);
      return [h.pending(PATH.TICKET)[0], G.g1];
    },
    deliver: answer(200, {})
  },
  "an earlier voice send's 200 that is no hand-off": {
    running: async (h) => {
      await listen(h);
      await voiceSend(h, "check the logs");
      await over(h);
      await listen(h, G.g2);
      return [h.pending(PATH.SEND)[0], G.g2];
    },
    closing: async (h) => { await listen(h); await voiceSend(h, "check the logs"); await cue(h); return [h.pending(PATH.SEND)[0], G.g1]; },
    deliver: answer(200, { request_id: "r1", state: "queued" })
  },
  "an abandoned grant request's 200": {
    running: async (h) => { const g = await abandonedGrant(h); await listen(h, G.g2, snap({ epoch: "e2" })); return [g, G.g2]; },
    closing: async (h) => {
      const g = await abandonedGrant(h);
      await listen(h, G.g2, snap({ epoch: "e2" }));
      await cue(h);
      return [g, G.g2];
    },
    deliver: answer(200, { grant: G.late, expires_in: 15 })
  }
};

test("every answer that cuts a closing cue also ends a listening conversation silently", async () => {
  for (const [name, a] of Object.entries(ANSWERS)) {
    const c = await paired();
    const [call, cueGrant] = await a.closing(c);
    assert.ok(call && !call.done, name + ": no open call while the cue plays");
    const cueUtt = c.utt();
    const heard = c.said.length;
    await a.deliver(c, call);
    assert.equal(c.synth.speaking, false, name + ": the closing cue still plays");
    assert.ok(cueUtt.over, name + ": the closing cue was not cut");
    assert.equal(c.said.length, heard, name + ": the page spoke");
    assert.ok(releases(c).includes(cueGrant), name + ": the cue's grant was not released");

    const h = await paired();
    const [open, grant] = await a.running(h);
    assert.ok(open && !open.done, name + ": no open call while listening");
    const rec = h.rec();
    const said = h.said.length;
    const released = releases(h).filter((g) => g === grant).length;
    await a.deliver(h, open);
    assert.match(h.state(), /^Conversation ended: /, name);
    assert.ok(rec.aborts > 0, name + ": the recognizer still listens");
    assert.equal(h.said.length, said, name + ": the page spoke");
    assert.equal(h.synth.speaking, false, name);
    assert.equal(releases(h).filter((g) => g === grant).length, released + 1, name + ": the grant was not released");
    rec.hear("stop listening");
    await settle();
    assert.equal(h.said.length, said, name + ": 'stop listening' started a cue");
  }
});

// Healthy answers from the same places leave both alone: silencing is for
// what the answer shows, not for where it came from.
test("a healthy snapshot from any source leaves the conversation listening and the cue playing", async () => {
  const healthy = Object.entries(ANSWERS).filter(([name]) => name.endsWith("showing the target gone"));
  for (const [name, a] of healthy) {
    const label = name.replace("showing the target gone", "showing the target usable");
    const c = await paired();
    const [call] = await a.closing(c);
    const heard = c.said.length;
    call.answer(200, snap());
    await settle();
    assert.equal(c.synth.speaking, true, label + ": the closing cue was cut");
    assert.equal(c.said.length, heard, label);
    const h = await paired();
    const [open] = await a.running(h);
    open.answer(200, snap());
    await settle();
    assert.equal(h.state(), "Listening.", label);
  }
});

// An earlier pairing's snapshot is no state of this one: another request on
// the session in it ends nothing, though the same in this pairing's poll would.
test("an earlier pairing's snapshot is never taken as the conversation's state", async () => {
  const h = await paired();
  const old = await repaired(h);
  await listen(h);
  old.answer(200, snap({ requests: [{ request_id: "x", session_id: "s1", state: "pending", text: "t", created: 1,
    confirm: "tap", reply: null }] }));
  await settle();
  assert.equal(h.state(), "Listening.");
});

// Each reproduction as the reviewer ran it.

test("a newer tap-flow refresh showing the target gone ends a listening conversation silently", async () => {
  const h = await paired();
  const refresh = await tapRefresh(h);
  const start = snap();
  await listen(h, G.g1, start);
  const rec = h.rec();
  const said = h.said.length;
  refresh.answer(200, snap({ version: start.version + 1, targets: [] }));
  await settle();
  assert.equal(h.state(), ended(REASON.TARGET));
  assert.ok(rec.aborts > 0, "the recognizer still listens");
  assert.deepEqual(releases(h), [G.g1]);
  rec.hear("stop listening");
  await settle();
  assert.equal(h.said.length, said, "a closing cue started");
});

test("a beat's 200 whose body is lost at its abort ends a listening conversation silently", async () => {
  const h = await paired();
  await listen(h);
  await h.advance(LIMITS.BEAT_MS);
  const rec = h.rec();
  const said = h.said.length;
  await h.headers(PATH.BEAT, 200);
  await h.advance(LIMITS.BEAT_ABORT_MS);
  assert.notEqual(h.state(), "Listening.");
  assert.ok(rec.aborts > 0, "the recognizer still listens");
  assert.deepEqual(releases(h), [G.g1]);
  rec.hear("stop listening");
  await settle();
  assert.equal(h.said.length, said, "a closing cue started");
});

test("an earlier pairing's poll whose body fails cuts the new pairing's closing cue", async () => {
  const h = await paired();
  await h.answer(POLL, 200, snap());
  const body = await h.headers(POLL, 200);
  await h.answer("/desk/api/startup", 401, { error: "unauthorized" });
  await h.advance(2000);
  assert.ok(h.$("pairing").hidden);
  await h.answer(PATH.STATE, 200, snap());
  await listen(h);
  await cue(h);
  const said = h.said.length;
  await body.lose();
  assert.equal(h.synth.speaking, false, "the closing cue still plays");
  assert.equal(h.said.length, said);
  assert.deepEqual(releases(h), [G.g1]);
});

test("a snapshot older than the conversation's, showing the target unusable, still ends it silently", async () => {
  const h = await paired();
  const older = snap({ sends_enabled: false });
  const refresh = await tapRefresh(h);
  await listen(h);
  refresh.answer(200, older);
  await settle();
  assert.equal(h.state(), ended(REASON.SENDS_OFF));
  assert.deepEqual(releases(h), [G.g1]);
});
