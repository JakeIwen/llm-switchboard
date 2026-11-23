// The conversation controller wired to fake recognition, speech, timers,
// wake lock and fetch, through its public surface only. Run by
// tests/test_desk_conversation_js.py with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../../llm_switchboard/desk/static/conversation_logic.js");
const C = require("../../llm_switchboard/desk/static/conversation.js");
const { PHASE, REASON, CUE, LIMITS } = L;
const { PATH } = C;

const { DEST, flush, target, snap, rq, harness, listening, sent, texts, releases } = require("./controller_harness.js");

test("a stale callback inside the current turn is dropped", async () => {
  const h = await listening(harness());
  const first = h.rec();
  await h.advance(2000);
  first.fire("end");
  await flush();
  const second = h.rec();
  assert.notEqual(second, first);
  first.result("hello");
  first.result("send it");
  first.fire("end");
  first.fire("audiostart");
  await flush();
  assert.equal(h.view().draft, "");
  assert.equal(h.phase(), PHASE.LISTENING);
  assert.equal(h.recs.length, 2);
  assert.equal(h.all(PATH.TICKET).length, 0);
});

test("the end of an aborted recognizer never reopens the mic", async () => {
  const h = await listening(harness());
  await h.hear("open the hatch");
  await h.hear("read it back");
  const aborted = h.recs[0];
  assert.equal(aborted.aborts, 1);
  assert.equal(h.phase(), PHASE.READING);
  aborted.fire("error", { error: "aborted" });
  aborted.fire("end");
  await flush();
  assert.equal(h.recs.length, 1);
  assert.equal(h.utt().text, "open the hatch");
  await h.finish();
  // The read-back ended: the mic opens only after the guard.
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.recs.length, 1);
  await h.advance(1);
  assert.equal(h.recs.length, 2);
  assert.equal(h.phase(), PHASE.LISTENING);
  assert.equal(h.view().draft, "open the hatch");
});

test("a ticket arriving after the end is ignored", async () => {
  const h = harness();
  h.honorAbort = false;
  await listening(h);
  await h.hear("x");
  await h.hear("send it");
  h.ctl.endTap();
  assert.equal(h.view().reason, REASON.END_TAPPED);
  await h.answer(PATH.TICKET, 200, { ticket: "late" });
  assert.equal(h.all(PATH.SEND).length, 0);
});

test("a grant arriving after End during starting is released at once", async () => {
  const h = harness();
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  h.ctl.endTap();
  assert.equal(h.phase(), PHASE.ENDED);
  assert.equal(h.sentinels[0].released, 1);
  await h.answer(PATH.GRANT, 200, { grant: "late-grant" });
  assert.deepEqual(releases(h), ["late-grant"]);
  assert.equal(h.recs.length, 0);
  assert.equal(h.said.length, 0);
});

test("a new Start never races an abandoned grant acquisition", async () => {
  const h = harness();
  const page = () => ({ recognizer: true, voice: true, tapSending: false, canStart: h.ctl.canStart(), blocker: h.ctl.blocker() });
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  const first = h.pending(PATH.GRANT)[0];
  h.ctl.endTap();
  assert.equal(h.ctl.canStart(), false);
  assert.equal(L.startBlocker(snap(), "s1", page()), L.BLOCKER.LETTING_GO);
  assert.equal(h.ctl.start(DEST), false);
  // The old acquisition reaches the hub and answers; its grant is released.
  first.answer(200, { grant: "old" });
  await flush();
  assert.deepEqual(releases(h), ["old"]);
  assert.equal(h.ctl.canStart(), false);
  await h.answer(PATH.RELEASE, 409, { error: "conversation grant not current" });
  assert.equal(h.ctl.canStart(), true);
  assert.equal(L.startBlocker(snap(), "s1", page()), null);
  // Only now does the next acquisition go out.
  const grantArrives = await startUntilGrant(h, "new");
  assert.equal(h.all(PATH.GRANT).length, 2);
  await grantArrives();
  await h.advance(LIMITS.GUARD_MS);
  assert.equal(h.phase(), PHASE.LISTENING);
  assert.deepEqual(releases(h), ["old"]);
});

test("an abandoned or failed grant request with no response holds Start until the hub's epoch changes", async () => {
  for (const ending of ["never answers", "fails without a response", "fails while still current"]) {
    const h = harness();
    h.ctl.start(DEST);
    await flush();
    await h.answer(PATH.STATE, 200, snap());
    const stuck = h.pending(PATH.GRANT)[0];
    if (ending === "fails while still current") {
      // A transport failure is no proof the hub never read it.
      stuck.fail();
      await flush();
      assert.equal(h.view().reason, REASON.HUB_UNREACHABLE, ending);
    } else {
      h.ctl.endTap();
    }
    if (ending === "fails without a response") { stuck.fail(); await flush(); }
    // No page-side timer lets it go: the hub may still read and run it.
    await h.advance(LIMITS.STEP_MS * 10);
    assert.equal(h.ctl.canStart(), false, ending);
    assert.equal(h.ctl.blocker(), L.BLOCKER.LETTING_GO, ending);
    assert.equal(stuck.aborted, undefined, ending);
    // A poll from the same hub process changes nothing.
    h.ctl.snapshot(snap());
    assert.equal(h.ctl.canStart(), false, ending);
    // A new epoch: the request named the old one and died with that process.
    h.ctl.snapshot(snap({ epoch: "e2" }));
    assert.equal(h.ctl.canStart(), true, ending);
    assert.equal(h.ctl.blocker(), null, ending);
  }
});

test("a retarget during starting ends it, at the tap or when the grant arrives", async () => {
  const h = harness();
  h.honorAbort = false;
  h.ctl.start(DEST);
  await flush();
  h.ctl.targetSelected("s1");
  assert.equal(h.phase(), PHASE.STARTING);
  h.ctl.targetSelected("s2");
  assert.equal(h.view().reason, REASON.RETARGET);
  await h.answer(PATH.STATE, 200, snap());
  assert.equal(h.sentinels.length, 0);

  // Selection changed without a notice: the grant's own check catches it,
  // and the page, still holding a fresh grant, says why and then releases it.
  const g = harness();
  g.ctl.start(DEST);
  await flush();
  await g.answer(PATH.STATE, 200, snap());
  g.selected = "s2";
  await g.answer(PATH.GRANT, 200, { grant: "g1" });
  assert.equal(g.view().reason, REASON.RETARGET);
  assert.equal(g.recs.length, 0);
  assert.deepEqual(texts(g), [CUE.ENDED + ": " + REASON.RETARGET + "."]);
  assert.deepEqual(releases(g), []);
  assert.equal(g.ctl.canStart(), false);
  await g.finish();
  assert.deepEqual(releases(g), ["g1"]);
});

test("a reply during the sent cue waits for the cue's end", async () => {
  const h = await listening(harness());
  await sent(h, "r1");
  h.ctl.snapshot(snap({ requests: [rq("r1", "answered", "It is done.")] }));
  await flush();
  assert.deepEqual(texts(h), [CUE.SENT]);
  await h.finish();
  assert.equal(h.phase(), PHASE.SPEAKING);
  assert.deepEqual(texts(h), [CUE.SENT, "It is done."]);
});

test("a poll that would speak a reply twice speaks it once", async () => {
  const h = await listening(harness());
  await sent(h, "r1");
  await h.finish();
  const answered = () => snap({ requests: [rq("r1", "answered", "Once only.")] });
  h.ctl.snapshot(answered());
  h.ctl.snapshot(answered());
  await flush();
  assert.deepEqual(texts(h), [CUE.SENT, "Once only."]);
  await h.finish();
  await h.advance(LIMITS.GUARD_MS);
  assert.equal(h.phase(), PHASE.LISTENING);
  h.ctl.snapshot(answered());
  await flush();
  assert.deepEqual(texts(h), [CUE.SENT, "Once only."]);
});

test("a late beat success does not renew the grant", async () => {
  const h = harness();
  h.honorAbort = false;
  await listening(h);
  await h.advance(LIMITS.BEAT_MS);
  const late = h.pending(PATH.BEAT)[0];
  assert.ok(late);
  await h.advance(LIMITS.BEAT_ABORT_MS + 500);
  late.answer(200, { grant: "g1" });
  await flush();
  // Fresh until 10 s after the grant request; the next beat never answers.
  await h.advance(LIMITS.FRESH_MS - LIMITS.BEAT_MS - LIMITS.BEAT_ABORT_MS - 500);
  assert.equal(h.phase(), PHASE.LISTENING);
  await h.advance(LIMITS.TICK_MS);
  assert.equal(h.view().reason, REASON.HUB_LOST);
  assert.equal(h.view().lost, true);
  // A page that lost its grant says nothing and still lets go of it.
  assert.equal(h.said.length, 0);
  assert.deepEqual(releases(h), ["g1"]);
});

test("a beat success that arrives after the deadline revives nothing", async () => {
  const h = await listening(harness());
  // The first beat is abandoned unanswered; the second starts at the deadline.
  await h.advance(LIMITS.FRESH_MS);
  assert.equal(h.phase(), PHASE.LISTENING);
  const second = h.pending(PATH.BEAT);
  assert.equal(second.length, 1);
  h.t += 100;
  second[0].answer(200, { grant: "g1", expires_in: 15 });
  await flush();
  assert.equal(h.view().reason, REASON.HUB_LOST);
  assert.equal(h.view().lost, true);
  assert.equal(h.rec().aborts, 1);
  assert.equal(h.said.length, 0);
});

test("an interim result after the deadline ends it before the tick runs", async () => {
  const h = await listening(harness());
  h.t += LIMITS.FRESH_MS + 1;
  h.rec().result("hel", false);
  await flush();
  assert.equal(h.view().reason, REASON.HUB_LOST);
  assert.equal(h.view().interim, "");
});

test("a refused beat means the grant is gone", async () => {
  const h = await listening(harness());
  await h.advance(LIMITS.BEAT_MS);
  await h.answer(PATH.BEAT, 409, { error: "conversation grant not current" });
  assert.equal(h.view().reason, REASON.GRANT_LOST);
  assert.equal(h.said.length, 0);
  assert.equal(h.rec().aborts, 1);
});

test("the request absent then present is followed to its reply", async () => {
  const h = await listening(harness());
  await sent(h, "r1");
  await h.finish();
  h.ctl.snapshot(snap());
  await h.alive(5000);
  assert.equal(h.phase(), PHASE.WAITING);
  h.ctl.snapshot(snap({ requests: [rq("r1", "handed_off")], targets: [target({ lock: { lock_id: "L", request_id: "r1" } })] }));
  await h.alive(LIMITS.ACK_WAIT_MS);
  assert.equal(h.phase(), PHASE.WAITING);
  h.ctl.snapshot(snap({ requests: [rq("r1", "answered", "Found it.")] }));
  await flush();
  assert.equal(h.utt().text, "Found it.");

  // Never present within 30 s of the ack: unknown.
  const n = await listening(harness());
  await sent(n, "r1");
  await n.finish();
  await n.alive(LIMITS.ACK_WAIT_MS);
  assert.equal(n.view().reason, REASON.UNKNOWN);
});

test("an answer that beats the POST is spoken after the sent cue", async () => {
  const h = await listening(harness());
  await h.hear("quick question");
  await h.hear("send it");
  await h.answer(PATH.TICKET, 200, { ticket: "t" });
  h.ctl.snapshot(snap({ requests: [rq("r1", "answered", "Yes.")] }));
  await flush();
  assert.equal(h.phase(), PHASE.SENDING);
  await h.answer(PATH.SEND, 200, { request_id: "r1", state: "answered" });
  assert.deepEqual(texts(h), [CUE.SENT]);
  await h.finish();
  assert.deepEqual(texts(h), [CUE.SENT, "Yes."]);
});

test("a foreign request seen while the ticket is outstanding ends it before any POST", async () => {
  const h = harness();
  h.honorAbort = false;
  await listening(h);
  await h.hear("x");
  await h.hear("send it");
  h.ctl.snapshot(snap({ requests: [rq("f1", "answered", "Someone else.")] }));
  await flush();
  assert.equal(h.view().reason, REASON.FOREIGN);
  await h.answer(PATH.TICKET, 200, { ticket: "t" });
  assert.equal(h.all(PATH.SEND).length, 0);
});

test("a send with no answer is unknown and never retried", async () => {
  const h = await listening(harness());
  await h.hear("x");
  await h.hear("send it");
  await h.answer(PATH.TICKET, 200, { ticket: "t" });
  await h.alive(LIMITS.SEND_DEADLINE_MS);
  assert.equal(h.view().reason, REASON.UNKNOWN);
  assert.equal(h.all(PATH.SEND).length, 1);
  assert.equal(h.all(PATH.TICKET).length, 1);

  const r = await listening(harness());
  await r.hear("x");
  await r.hear("send it");
  await r.answer(PATH.TICKET, 200, { ticket: "t" });
  await r.answer(PATH.SEND, 409, { error: "conversation grant not current" });
  assert.equal(r.view().reason, "not sent: conversation grant not current");
});

test("an end that abandons an unanswered send POST also says delivery is unknown", async () => {
  const unknown = (reason) => reason + "; " + REASON.UNKNOWN;
  // The hub goes quiet with the POST out: the freshness deadline ends it first.
  const h = await listening(harness());
  await h.hear("check the logs");
  await h.hear("send it");
  await h.answer(PATH.TICKET, 200, { ticket: "t" });
  assert.equal(h.pending(PATH.SEND).length, 1);
  await h.advance(LIMITS.FRESH_MS + LIMITS.TICK_MS);
  assert.equal(h.view().reason, unknown(REASON.HUB_LOST));
  assert.equal(h.view().lost, true);
  assert.equal(h.said.length, 0);
  assert.equal(h.all(PATH.SEND).length, 1);
  assert.equal(h.all(PATH.TICKET).length, 1);

  // The same with the ticket still out: no POST was issued, only the cause.
  const t = await listening(harness());
  await t.hear("check the logs");
  await t.advance(2000);
  await t.hear("send it");
  await t.advance(LIMITS.FRESH_MS - 2000 + LIMITS.TICK_MS);
  assert.equal(t.pending(PATH.TICKET).length, 0);
  assert.equal(t.view().reason, REASON.HUB_LOST);
  assert.equal(t.all(PATH.SEND).length, 0);

  // End tapped with the POST out: the closing cue says it too.
  const e = await listening(harness());
  await e.hear("check the logs");
  await e.hear("send it");
  await e.answer(PATH.TICKET, 200, { ticket: "t" });
  e.ctl.endTap();
  assert.equal(e.view().reason, unknown(REASON.END_TAPPED));
  assert.deepEqual(texts(e), [CUE.ENDED + ": " + unknown(REASON.END_TAPPED) + "."]);

  // An answered refusal is definite: not sent, nothing more.
  const r = await listening(harness());
  await r.hear("x");
  await r.hear("send it");
  await r.answer(PATH.TICKET, 200, { ticket: "t" });
  await r.answer(PATH.SEND, 409, { error: "busy" });
  assert.equal(r.view().reason, L.withDetail(REASON.NOT_SENT, "busy"));
});

test("a hub answer that proves the grant gone ends it silently on every call path", async () => {
  for (const [path, status, error] of [
    [PATH.SEND, 409, "conversation grant not current"],
    [PATH.SEND, 409, "conversation grant not for this target"],
    [PATH.SEND, 409, "stale epoch"],
    [PATH.TICKET, 409, "stale generation"],
  ]) {
    const h = await listening(harness());
    await h.hear("x");
    await h.hear("send it");
    if (path === PATH.SEND) await h.answer(PATH.TICKET, 200, { ticket: "t" });
    await h.answer(path, status, { error: error });
    assert.equal(h.view().reason, L.withDetail(REASON.NOT_SENT, error), error);
    assert.equal(h.view().lost, true, error);
    assert.equal(h.said.length, 0, error);
    assert.deepEqual(releases(h), ["g1"], error);
  }
  // A poll showing a new epoch or generation: the grant died with it.
  for (const s of [() => snap({ epoch: "e2" }), () => snap({ targets: [target({ generation: 4 })] })]) {
    const h = await listening(harness());
    h.ctl.snapshot(s());
    await flush();
    assert.equal(h.view().lost, true);
    assert.equal(h.said.length, 0);
    assert.deepEqual(releases(h), ["g1"]);
  }
  // A refusal that says nothing about the grant still gets its closing cue.
  const b = await listening(harness());
  await b.hear("x");
  await b.hear("send it");
  await b.answer(PATH.TICKET, 200, { ticket: "t" });
  await b.answer(PATH.SEND, 409, { error: "busy" });
  assert.equal(b.view().lost, false);
  assert.deepEqual(texts(b), [CUE.ENDED + ": " + L.withDetail(REASON.NOT_SENT, "busy") + "."]);
});

test("losing the sound slot is decided apart from the reason shown, and nothing is spoken after it", async () => {
  // The hub's own handoff refusal for a changed registration.
  const r = await listening(harness());
  await r.hear("x");
  await r.hear("send it");
  await r.answer(PATH.TICKET, 200, { ticket: "t" });
  await r.answer(PATH.SEND, 409, { error: "target changed; not sent" });
  assert.equal(r.view().reason, L.withDetail(REASON.NOT_SENT, "target changed; not sent"));
  assert.equal(r.view().lost, true);
  assert.equal(r.said.length, 0);
  assert.deepEqual(releases(r), ["g1"]);

  for (const [name, s, reason] of [
    ["the target gone altogether", () => snap({ targets: [] }), REASON.TARGET],
    ["a new generation with voice confirm off", () => snap({ voice_confirm_enabled: false, targets: [target({ generation: 4 })] }),
      REASON.VOICE_OFF],
    ["a new epoch with sends off", () => snap({ epoch: "e2", sends_enabled: false }), REASON.EPOCH],
  ]) {
    const h = await listening(harness());
    await h.hear("half a thought");
    h.ctl.snapshot(s());
    await flush();
    assert.equal(h.view().reason, reason, name);
    assert.equal(h.view().lost, true, name);
    assert.equal(h.said.length, 0, name);
    assert.deepEqual(releases(h), ["g1"], name);

    // The same snapshot during the closing cue cuts it: uncertain, and let go.
    const g = await listening(harness());
    await g.hear("stop listening");
    assert.equal(g.said.length, 1, name);
    g.ctl.snapshot(s());
    await flush();
    assert.equal(g.synth.cancels, 1, name);
    assert.deepEqual(releases(g), ["g1"], name);
    assert.equal(g.ctl.canStart(), false, name);
    assert.equal(g.said.length, 1, name);
  }
  // A switch turned off with the registration intact keeps the cue.
  const v = await listening(harness());
  v.ctl.snapshot(snap({ voice_confirm_enabled: false }));
  await flush();
  assert.equal(v.view().lost, false);
  assert.deepEqual(texts(v), [CUE.ENDED + ": " + REASON.VOICE_OFF + "."]);
});

test("an utterance that never ends is cancelled, ends it, and holds Start for the quiet period", async () => {
  const h = await listening(harness());
  await h.hear("one two three four five");
  await h.hear("read it back");
  const deadline = L.estimateMs("one two three four five") * L.SPEECH_DEADLINE_FACTOR;
  await h.alive(deadline - 500);
  assert.equal(h.phase(), PHASE.READING);
  await h.alive(500);
  assert.equal(h.view().reason, REASON.SPEECH);
  assert.equal(h.synth.cancels, 1);
  // Cut off: no closing cue; the grant is let go at once.
  assert.equal(h.said.length, 1);
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.ctl.canStart(), false);
  assert.equal(h.ctl.start(DEST), false);
  await h.advance(deadline - 1);
  assert.equal(h.ctl.canStart(), false);
  await h.advance(1);
  assert.equal(h.ctl.canStart(), true);
});

test("a draft overflow clears the whole draft with a cue", async () => {
  const h = await listening(harness());
  await h.hear("a".repeat(1000));
  await h.hear("b".repeat(30));
  assert.equal(h.view().draft, "");
  assert.equal(h.phase(), PHASE.READING);
  assert.equal(h.recs[0].aborts, 1);
  assert.equal(h.utt().text, CUE.OVERFLOW);
  await h.finish();
  await h.advance(LIMITS.GUARD_MS);
  assert.equal(h.phase(), PHASE.LISTENING);
  await h.hear("send it");
  assert.equal(h.utt().text, CUE.NOTHING_TO_SEND);
});

test("a target with an answered request completes two spoken turns", async () => {
  const h = await listening(harness(), snap({ requests: [rq("r0", "answered", "Old reply.")] }));
  const history = [rq("r0", "answered", "Old reply.")];
  for (const id of ["r1", "r2"]) {
    await sent(h, id, "question " + id);
    await h.finish();
    history.push(rq(id, "answered", "Reply " + id + "."));
    h.ctl.snapshot(snap({ requests: history.slice() }));
    await flush();
    assert.equal(h.utt().text, "Reply " + id + ".");
    await h.finish();
    await h.advance(LIMITS.GUARD_MS);
    assert.equal(h.phase(), PHASE.LISTENING);
    assert.equal(h.view().draft, "");
  }
  assert.deepEqual(texts(h), [CUE.SENT, "Reply r1.", CUE.SENT, "Reply r2."]);
});

test("new foreign activity during a turn ends it, closes the mic and says why", async () => {
  const h = await listening(harness());
  await h.hear("half a thought");
  h.ctl.snapshot(snap({ requests: [rq("r9", "answered", "Someone else.")] }));
  await flush();
  assert.equal(h.view().reason, REASON.FOREIGN);
  assert.equal(h.view().draft, "");
  assert.equal(h.rec().aborts, 1);
  assert.deepEqual(texts(h), [CUE.ENDED + ": " + REASON.FOREIGN + "."]);
  await h.finish();
  assert.deepEqual(releases(h), ["g1"]);
});

test("hide, return and Start while an utterance is playing is refused until it must be over", async () => {
  const h = await listening(harness());
  await sent(h, "r1");
  await h.finish();
  h.ctl.snapshot(snap({ requests: [rq("r1", "answered", "A long reply that is still playing.")] }));
  await flush();
  assert.equal(h.phase(), PHASE.SPEAKING);
  h.visible = false;
  h.ctl.hidden();
  assert.equal(h.view().reason, REASON.HIDDEN);
  assert.equal(h.synth.cancels, 1);
  assert.deepEqual(releases(h), ["g1"]);
  h.visible = true;
  assert.equal(h.ctl.start(DEST), false);
  assert.equal(h.phase(), PHASE.ENDED);
  // The cut utterance's own end never arrives; its quiet period does.
  await h.advance(L.estimateMs("A long reply that is still playing.") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(h.ctl.start(DEST), true);
});

test("a start() with no audiostart is ended by the watchdog", async () => {
  const h = harness();
  h.autoAudio = false;
  await listening(h);
  await h.alive(LIMITS.WATCHDOG_MS);
  assert.equal(h.view().reason, REASON.REC_HUNG);
  assert.equal(h.rec().aborts, 1);
});

test("a silent session ends with nomatch then end, and recognition restarts", async () => {
  const h = await listening(harness());
  await h.alive(5100);
  h.rec().fire("nomatch");
  h.rec().fire("end");
  await flush();
  assert.equal(h.recs.length, 2);
  assert.equal(h.recs[1].starts, 1);
  assert.equal(h.phase(), PHASE.LISTENING);
});

test("a no-speech error followed by end restarts recognition", async () => {
  const h = await listening(harness());
  await h.alive(3000);
  h.rec().fire("error", { error: "no-speech" });
  h.rec().fire("end");
  await flush();
  assert.equal(h.recs.length, 2);
  assert.equal(h.phase(), PHASE.LISTENING);
  h.rec().fire("error", { error: "network" });
  await flush();
  assert.equal(h.view().reason, "speech recognition failed: network");
});

test("a recognizer that refuses to start ends it", async () => {
  const h = harness();
  h.recThrows = true;
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.answer(PATH.GRANT, 200, { grant: "g1" });
  assert.equal(h.view().reason, REASON.REC_FAILED);
});

test("Start right after a completed closing cue waits out the guard", async () => {
  const h = await listening(harness());
  await h.hear("stop listening");
  assert.deepEqual(texts(h), [CUE.ENDED + ": " + REASON.STOPPED + "."]);
  assert.equal(h.ctl.canStart(), false);
  await h.finish();
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.ctl.start(DEST), true);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.answer(PATH.GRANT, 200, { grant: "g2" });
  assert.equal(h.phase(), PHASE.STARTING);
  assert.equal(h.recs.length, 1);
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.recs.length, 1);
  await h.advance(1);
  assert.equal(h.recs.length, 2);
  assert.equal(h.phase(), PHASE.LISTENING);
});

test("the page's own utterances hold Start and set the guard", async () => {
  const h = harness();
  const u = new h.Utterance("Play this reply.");
  h.ctl.track(u);
  assert.equal(h.ctl.canStart(), false);
  u.fire("end");
  assert.equal(h.ctl.canStart(), true);
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.answer(PATH.GRANT, 200, { grant: "g1" });
  assert.equal(h.recs.length, 0);
  await h.advance(LIMITS.GUARD_MS);
  assert.equal(h.recs.length, 1);

  // Cancelled by the page: uncertain for twice its estimate.
  const g = harness();
  const v = new g.Utterance("Cut me off.");
  g.ctl.track(v);
  v.fire("error", { error: "interrupted" });
  assert.equal(g.ctl.canStart(), false);
  await g.advance(L.estimateMs("Cut me off.") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(g.ctl.canStart(), true);
});

// Starts a conversation whose grant answers at the current instant.
async function startUntilGrant(h, grantId) {
  assert.equal(h.ctl.start(DEST), true);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  assert.equal(h.pending(PATH.GRANT).length, 1);
  return async () => h.answer(PATH.GRANT, 200, { grant: grantId || "g1" });
}

test("a Play that outlives its deadline is cancelled, and its real end still holds the next mic for the guard", async () => {
  const h = harness();
  // One long number is one word to the estimator.
  const u = new h.Utterance("4815162342");
  h.ctl.track(u);
  const deadline = L.estimateMs(u.text) * L.SPEECH_DEADLINE_FACTOR;
  await h.advance(deadline);
  assert.equal(h.synth.cancels, 1);
  assert.equal(h.ctl.canStart(), false);
  await h.advance(deadline);
  assert.equal(h.ctl.canStart(), true);
  const grantArrives = await startUntilGrant(h);
  // Its real end comes now, at the very instant the grant arrives.
  u.fire("end");
  await grantArrives();
  assert.equal(h.recs.length, 0);
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.recs.length, 0);
  await h.advance(1);
  assert.equal(h.recs.length, 1);
  assert.equal(h.phase(), PHASE.LISTENING);
});

test("a superseded page utterance's watchdog is disarmed, and a cancel goes through the tracker", async () => {
  const h = harness();
  const first = new h.Utterance("Short.");
  h.ctl.track(first);
  await h.advance(L.estimateMs("Short.") * L.SPEECH_DEADLINE_FACTOR - 50);
  // Submitted without a cancel in between: the newer one supersedes it.
  const second = new h.Utterance("A much longer utterance that is still playing.");
  h.ctl.track(second);
  await h.advance(100);
  assert.equal(h.synth.cancels, 0, "the superseded watchdog cancelled the newer speech");
  // The page's own cancel marks what it stopped and disarms its watchdog.
  h.ctl.stopPageSpeech();
  assert.equal(h.synth.cancels, 1);
  assert.equal(h.ctl.canStart(), false);
  await h.advance(L.estimateMs(second.text) * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(h.synth.cancels, 1);
  assert.equal(h.ctl.canStart(), true);
});

// A Play the watchdog cancelled, whose real end never came, then its quiet
// period: Start is allowed again.
async function writtenOff(h) {
  const play = new h.Utterance("4815162342");
  h.ctl.track(play);
  const deadline = L.estimateMs(play.text) * L.SPEECH_DEADLINE_FACTOR;
  await h.advance(deadline);
  assert.equal(h.synth.cancels, 1);
  await h.advance(deadline);
  assert.equal(h.ctl.canStart(), true);
  return play;
}

test("a late end of written-off speech ends the conversation silently and its draft is never sent", async () => {
  for (const late of ["end", "error"]) {
    const h = harness();
    const play = await writtenOff(h);
    await listening(h);
    await h.hear("check the logs");
    const mic = h.rec();
    play.fire(late, { error: "interrupted" });
    await flush();
    assert.equal(h.view().reason, REASON.ECHO, late);
    assert.equal(h.view().draft, "", late);
    assert.equal(mic.aborts, 1, late);
    assert.equal(h.said.length, 0, late);
    assert.deepEqual(releases(h), ["g1"], late);
    // The desk may still have been speaking: Start waits out a new quiet period.
    assert.equal(h.ctl.canStart(), false, late);
    mic.result("send it");
    await flush();
    assert.equal(h.all(PATH.TICKET).length, 0, late);
  }

  // An open mic with nothing drafted yet ends too.
  const m = harness();
  const play = await writtenOff(m);
  await listening(m);
  play.fire("end");
  await flush();
  assert.equal(m.view().reason, REASON.ECHO);

  // The draft frozen for sending, its ticket still out: nothing is posted.
  const s = harness();
  s.honorAbort = false;
  const sending = await writtenOff(s);
  await listening(s);
  await s.hear("check the logs");
  await s.hear("send it");
  assert.equal(s.phase(), PHASE.SENDING);
  sending.fire("end");
  await flush();
  assert.equal(s.view().reason, REASON.ECHO);
  await s.answer(PATH.TICKET, 200, { ticket: "t" });
  assert.equal(s.all(PATH.SEND).length, 0);
});

test("the voice list emptying during the start's guard keeps the mic closed", async () => {
  const h = harness();
  const u = new h.Utterance("Play this.");
  h.ctl.track(u);
  u.fire("end");
  const grantArrives = await startUntilGrant(h);
  await grantArrives();
  assert.equal(h.recs.length, 0);
  h.voice = false;
  await h.advance(LIMITS.GUARD_MS);
  assert.equal(h.recs.length, 0);
  assert.equal(h.view().reason, REASON.NO_VOICE);
  assert.deepEqual(releases(h), ["g1"]);
});

test("a conversation utterance's real end after its cut still holds the next mic for the guard", async () => {
  const h = await listening(harness());
  await h.hear("one two");
  await h.hear("read it back");
  const readback = h.utt();
  const deadline = L.estimateMs("one two") * L.SPEECH_DEADLINE_FACTOR;
  await h.alive(deadline);
  assert.equal(h.view().reason, REASON.SPEECH);
  await h.advance(deadline);
  assert.equal(h.ctl.canStart(), true);
  const grantArrives = await startUntilGrant(h, "g2");
  readback.fire("end");
  await grantArrives();
  assert.equal(h.recs.length, 1);
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.recs.length, 1);
  await h.advance(1);
  assert.equal(h.recs.length, 2);
});

test("a conversation utterance's error cancels the voice as cleanup and holds Start", async () => {
  const h = await listening(harness());
  await h.hear("one two");
  await h.hear("read it back");
  h.utt().fire("error", { error: "synthesis-failed" });
  await flush();
  assert.equal(h.view().reason, REASON.SPEECH);
  assert.equal(h.synth.cancels, 1);
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.ctl.canStart(), false);
  await h.advance(L.estimateMs("one two") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(h.ctl.canStart(), true);
});

test("Dictate's sound check waits for every utterance, the guard after its end and any uncertain period", async () => {
  const h = harness();
  assert.equal(h.ctl.soundClear(), true);
  const u = new h.Utterance("Play this reply.");
  h.ctl.track(u);
  assert.equal(h.ctl.soundClear(), false);
  h.t += 2000;
  u.fire("end");
  assert.equal(h.ctl.soundClear(), false);
  h.changes.length = 0;
  await h.advance(LIMITS.GUARD_MS - 1);
  assert.equal(h.ctl.soundClear(), false);
  await h.advance(1);
  assert.equal(h.ctl.soundClear(), true);
  // The page is told when it turned true.
  assert.equal(h.changes[h.changes.length - 1].soundClear, true);

  // A cut-off conversation cue: closed until its quiet period is over.
  const g = await listening(harness());
  await g.hear("stop listening");
  assert.equal(g.ctl.soundClear(), false);
  g.ctl.endTap();
  assert.equal(g.ctl.holds(), false);
  assert.equal(g.ctl.soundClear(), false);
  const quiet = L.estimateMs(texts(g)[0]) * L.SPEECH_DEADLINE_FACTOR;
  await g.advance(quiet - 1);
  assert.equal(g.ctl.soundClear(), false);
  await g.advance(1);
  assert.equal(g.ctl.soundClear(), true);
  assert.equal(g.changes[g.changes.length - 1].soundClear, true);
});

test("the wake lock released by the system ends it", async () => {
  const h = await listening(harness());
  h.sentinels[0].listeners.forEach((fn) => fn());
  await flush();
  assert.equal(h.view().reason, REASON.WAKE);

  const n = harness();
  n.wakeFails = true;
  n.ctl.start(DEST);
  await flush();
  await n.answer(PATH.STATE, 200, snap());
  assert.equal(n.view().reason, REASON.NO_WAKE);
  assert.equal(n.all(PATH.GRANT).length, 0);
});

test("a refused grant ends the start with the hub's reason and speaks nothing", async () => {
  const h = harness();
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.answer(PATH.GRANT, 409, { error: "another page is paired; revoke its token first" });
  assert.equal(h.view().reason, REASON.REFUSED + ": another page is paired; revoke its token first");
  assert.equal(h.said.length, 0);
  assert.equal(h.sentinels[0].released, 1);
});

test("a hung start step fails, and its late grant is still released", async () => {
  const h = harness();
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.advance(LIMITS.STEP_MS);
  assert.equal(h.view().reason, REASON.HUB_UNREACHABLE);
  await h.answer(PATH.GRANT, 200, { grant: "slow" });
  assert.deepEqual(releases(h), ["slow"]);
});

test("leaving the page ends it silently whatever visibilityState says, and still releases the grant", async () => {
  const h = await listening(harness());
  await h.hear("half a thought");
  // pagehide with the page still reporting visible.
  h.ctl.pageExit();
  assert.equal(h.view().reason, REASON.EXITED);
  assert.equal(h.said.length, 0);
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.ctl.holds(), false);

  // Leaving during the closing cue cuts it and lets the grant go.
  const g = await listening(harness());
  await g.hear("stop listening");
  assert.deepEqual(texts(g), [CUE.ENDED + ": " + REASON.STOPPED + "."]);
  assert.deepEqual(releases(g), []);
  g.ctl.pageExit();
  assert.equal(g.synth.cancels, 1);
  assert.deepEqual(releases(g), ["g1"]);
  assert.equal(g.ctl.holds(), false);
  assert.equal(g.ctl.canStart(), false);
});

test("the closing cue is beaten while it plays and never outlives the grant", async () => {
  // The hub answers: the cue plays past the old deadline to its end.
  const h = await listening(harness());
  await h.alive(4000);
  await h.hear("stop listening");
  const beatsBefore = h.all(PATH.BEAT).length;
  // Past the deadline the last beat before the end set (11 s), not past the cue's own.
  await h.alive(6500);
  assert.ok(h.all(PATH.BEAT).length > beatsBefore);
  assert.equal(h.synth.cancels, 0);
  assert.deepEqual(releases(h), []);
  await h.finish();
  assert.deepEqual(releases(h), ["g1"]);
  const after = h.all(PATH.BEAT).length;
  await h.advance(LIMITS.BEAT_MS * 2);
  assert.equal(h.all(PATH.BEAT).length, after);

  // The hub goes quiet: the cue is cut when the grant's deadline passes.
  const q = await listening(harness());
  await q.advance(6000);
  await q.hear("stop listening");
  assert.equal(q.said.length, 1);
  await q.advance(LIMITS.FRESH_MS - 6000 + LIMITS.TICK_MS);
  assert.equal(q.synth.cancels, 1);
  assert.deepEqual(releases(q), ["g1"]);
  assert.equal(q.ctl.holds(), false);
  assert.equal(q.ctl.canStart(), false);

  // The hub refuses the beat: the cue is cut at once.
  const r = await listening(harness());
  await r.hear("stop listening");
  await r.advance(LIMITS.BEAT_MS);
  await r.answer(PATH.BEAT, 409, { error: "conversation grant not current" });
  assert.equal(r.synth.cancels, 1);
  assert.deepEqual(releases(r), ["g1"]);

  // A poll showing the hub restarted: the cue is cut at once.
  const e = await listening(harness());
  await e.hear("stop listening");
  e.ctl.snapshot(snap({ epoch: "e2" }));
  assert.equal(e.synth.cancels, 1);
  assert.deepEqual(releases(e), ["g1"]);
});

test("unpairing ends it as a lost grant, with no closing cue", async () => {
  const h = await listening(harness());
  h.ctl.unpaired();
  assert.equal(h.view().reason, REASON.UNPAIRED);
  assert.equal(h.said.length, 0);
});
