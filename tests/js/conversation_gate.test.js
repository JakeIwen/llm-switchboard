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
  STATIC, L, PATH, REASON, POLL, settle, paired, snap, target, ended, releases, voiceSend
} = require("./page_harness.js");
const { LIMITS } = L;

const failed = (status) => L.withDetail(REASON.CALL_FAILED, "HTTP " + status);
const last = (h, p) => h.pending(p)[h.pending(p).length - 1];

// A Start tap through to the open mic, answering the newest Start snapshot
// and grant request, so older calls on the same paths stay open.
async function listen(h, grant, s) {
  if (h.$("conv-end").textContent === "Close") h.$("conv-end").click();
  h.$("conv-start").click();
  await settle();
  last(h, PATH.STATE).answer(200, s || snap());
  await settle();
  last(h, PATH.GRANT).answer(200, { grant: grant || G.g1, expires_in: 15 });
  await settle();
  assert.equal(h.state(), "Listening.");
}

// End tapped: the closing cue plays.
async function cue(h) {
  h.$("conv-end").click();
  await settle();
  assert.match(h.utt().text, /^Conversation ended: End was tapped/);
  assert.equal(h.synth.speaking, true);
}

// A conversation ended by its closing cue's own end, the guard passed.
async function over(h) {
  await cue(h);
  h.utt().fire("end");
  await settle();
  await h.advance(LIMITS.GUARD_MS);
}

async function drafted(h) {
  h.rec().hear("check the logs");
  h.rec().hear("send it");
  await settle();
}

// For every call the page can make, how to have it open while a closing
// cue plays (`closing`) and while a conversation runs (`running`), as the
// call to answer; `starting` when the running conversation holds no grant
// yet. Two calls are left out because neither can be open then: the
// pairing calls are made only while unpaired, which ends any conversation
// and its cue first, and Start waits for a tapped send until it settles
// (the "Send" control is off while a conversation or its cue holds the page).
const CALLS = {
  "the poll": {
    running: async (h) => { await listen(h); return h.pending(POLL)[0]; },
    closing: async (h) => { await listen(h); await cue(h); return h.pending(POLL)[0]; }
  },
  "the Start snapshot": {
    starting: true,
    running: async (h) => { h.$("conv-start").click(); await settle(); return h.pending(PATH.STATE)[0]; }
  },
  "an abandoned Start snapshot": {
    running: async (h) => { const s = await abandonedSnapshot(h); await listen(h); return s; },
    closing: async (h) => { const s = await abandonedSnapshot(h); await listen(h); await cue(h); return s; }
  },
  "the grant request": {
    starting: true,
    running: async (h) => {
      h.$("conv-start").click();
      await settle();
      await h.answer(PATH.STATE, 200, snap());
      return h.pending(PATH.GRANT)[0];
    }
  },
  "an abandoned grant request": {
    running: async (h) => { const g = await abandonedGrant(h); await listen(h, G.g2, snap({ epoch: "e2" })); return g; },
    closing: async (h) => {
      const g = await abandonedGrant(h);
      await listen(h, G.g2, snap({ epoch: "e2" }));
      await cue(h);
      return g;
    }
  },
  "the beat": {
    running: async (h) => { await listen(h); await h.advance(LIMITS.BEAT_MS); return h.pending(PATH.BEAT)[0]; },
    closing: async (h) => { await listen(h); await cue(h); await h.advance(LIMITS.BEAT_MS); return h.pending(PATH.BEAT)[0]; }
  },
  "a beat past its abort": {
    running: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await h.advance(LIMITS.BEAT_MS + LIMITS.BEAT_ABORT_MS);
      return h.pending(PATH.BEAT)[0];
    },
    closing: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await h.advance(LIMITS.BEAT_MS + LIMITS.BEAT_ABORT_MS - 500);
      await cue(h);
      await h.advance(500);
      return h.pending(PATH.BEAT)[0];
    }
  },
  "an earlier conversation's grant release": {
    running: async (h) => { await listen(h); await over(h); await listen(h, G.g2); return h.pending(PATH.RELEASE)[0]; },
    closing: async (h) => {
      await listen(h);
      await over(h);
      await listen(h, G.g2);
      await cue(h);
      return h.pending(PATH.RELEASE)[0];
    }
  },
  "the voice ticket": {
    running: async (h) => { await listen(h); await drafted(h); return h.pending(PATH.TICKET)[0]; }
  },
  "an abandoned voice ticket": {
    running: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await drafted(h);
      await over(h);
      await listen(h, G.g2);
      return h.pending(PATH.TICKET)[0];
    },
    closing: async (h) => { h.honorAbort = false; await listen(h); await drafted(h); await cue(h); return h.pending(PATH.TICKET)[0]; }
  },
  "the voice send": {
    running: async (h) => { await listen(h); await voiceSend(h, "check the logs"); return h.pending(PATH.SEND)[0]; },
    closing: async (h) => { await listen(h); await voiceSend(h, "check the logs"); await cue(h); return h.pending(PATH.SEND)[0]; }
  },
  "an earlier conversation's voice send": {
    running: async (h) => {
      await listen(h);
      await voiceSend(h, "check the logs");
      await over(h);
      await listen(h, G.g2);
      return h.pending(PATH.SEND)[0];
    }
  },
  "the tap flow's ticket": {
    running: async (h) => { tapTicket(h); await listen(h); return h.pending("/desk/api/ticket")[0]; },
    closing: async (h) => { tapTicket(h); await listen(h); await cue(h); return h.pending("/desk/api/ticket")[0]; }
  },
  "the tap flow's state refresh": {
    running: async (h) => { const r = await tapRefresh(h); await listen(h); return r; },
    closing: async (h) => { const r = await tapRefresh(h); await listen(h); await cue(h); return r; }
  },
  "the startup listing": {
    running: async (h) => { await listen(h); return h.pending("/desk/api/startup")[0]; },
    closing: async (h) => { await listen(h); await cue(h); return h.pending("/desk/api/startup")[0]; }
  },
  "a startup acknowledgement": {
    running: async (h) => { await ack(h); await listen(h); return h.pending("/desk/api/startup/ack")[0]; },
    closing: async (h) => { await ack(h); await listen(h); await cue(h); return h.pending("/desk/api/startup/ack")[0]; }
  },
  "a lock clear": {
    running: async (h) => { await clearLock(h); await listen(h); return h.pending("/desk/api/clear")[0]; },
    closing: async (h) => { await clearLock(h); await listen(h); await cue(h); return h.pending("/desk/api/clear")[0]; }
  },
  "an unenroll": {
    running: async (h) => { await listen(h); unenroll(h); return h.pending("/desk/api/unenroll")[0]; },
    closing: async (h) => { await listen(h); await cue(h); unenroll(h); return h.pending("/desk/api/unenroll")[0]; }
  }
};

// A Start snapshot given up on at the step deadline, its abort lost.
async function abandonedSnapshot(h) {
  h.honorAbort = false;
  h.$("conv-start").click();
  await settle();
  const s = h.pending(PATH.STATE)[0];
  await h.advance(LIMITS.STEP_MS);
  assert.equal(h.state(), ended(REASON.HUB_UNREACHABLE));
  h.honorAbort = true;
  return s;
}

// A grant request given up on at the step deadline, let go when a poll
// showed a new hub epoch.
async function abandonedGrant(h) {
  h.$("conv-start").click();
  await settle();
  await h.answer(PATH.STATE, 200, snap());
  const g = h.pending(PATH.GRANT)[0];
  await h.advance(LIMITS.STEP_MS);
  await h.answer(POLL, 200, snap({ epoch: "e2" }));
  return g;
}

function tapTicket(h) {
  h.$("draft").value = "hello";
  h.$("draft").dispatch("input");
}

// A tapped send handed off: its state refresh is left open.
async function tapRefresh(h) {
  tapTicket(h);
  await h.answer("/desk/api/ticket", 200, { ticket: "tap-ticket" });
  h.$("review-btn").click();
  h.$("send-btn").click();
  await settle();
  await h.answer("/desk/api/send", 200, { request_id: "tap1", state: "handed_off" });
  return h.pending(PATH.STATE)[0];
}

async function ack(h) {
  await h.answer("/desk/api/startup", 200, { sessions: [{ session_id: "s1", digest: "d1", capture: "warning",
    refusal: null }] });
  h.$("startup-list").all((k) => k.getAttribute("data-testid") === "startup-ack")[0].click();
  await settle();
}

async function clearLock(h) {
  await h.answer(POLL, 200, snap({ targets: [target("s1", { lock: { lock_id: "k1", request_id: "r0", since: 1 } })] }));
  h.$("clear-btn").click();
  h.$("clear-yes").click();
  await settle();
  await h.answer(POLL, 200, snap());
}

function unenroll(h) {
  h.$("enroll-list").all((k) => k.getAttribute("data-testid") === "unenroll-btn")[0].click();
}

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
