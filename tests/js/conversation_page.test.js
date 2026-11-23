// The tablet page's conversation wiring, in the page harness
// (page_harness.js). Asserts through what the page shows and what it sends.
// Run by tests/test_desk_conversation_js.py with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  L, PATH, REASON, BLOCKER, CUE, POLL, settle, paired, converse, snap, target, rq, voiceRq, ended, releases,
  playFor, slowCancel, voiceSend
} = require("./page_harness.js");

test("a Start tap voids an open review and its ticket", async () => {
  const h = await paired();
  h.$("draft").value = "hello";
  h.$("draft").dispatch("input");
  await h.answer("/desk/api/ticket", 200, { ticket: "tap-ticket" });
  h.$("review-btn").click();
  assert.equal(h.$("review").hidden, false);
  await converse(h);
  assert.equal(h.$("review").hidden, true);
  // The conversation's own ticket is requested later; the tap flow's is gone.
  h.$("send-btn").click();
  await settle();
  assert.equal(h.all("/desk/api/send").length, 0);
});

test("a Start tap stops one-shot dictation", async () => {
  const h = await paired();
  h.$("dictate").click();
  await h.answer("/desk/api/ticket", 200, { ticket: "tap-ticket" });
  const dictation = h.rec();
  assert.equal(dictation.starts, 1);
  await converse(h);
  assert.equal(dictation.aborts, 1);
  assert.equal(h.$("dictate").textContent, "Dictate");
  assert.notEqual(h.rec(), dictation, "the conversation opens its own recognizer");
});

test("selecting another target ends the conversation, and its closing cue is not cut", async () => {
  const two = () => snap({ targets: [target("s1"), target("s2")] });
  const h = await paired(two());
  h.target("s1").click();
  await converse(h, two());
  const cancels = h.synth.cancels;
  h.target("s2").click();
  await settle();
  assert.equal(h.state(), ended(REASON.RETARGET));
  assert.equal(h.utt().text, ended(REASON.RETARGET));
  assert.equal(h.synth.cancels, cancels, "the page cancelled the controller's closing cue");
  assert.deepEqual(releases(h), []);
  h.utt().fire("end");
  await settle();
  assert.deepEqual(releases(h), ["g1"]);
});

test("hiding the page ends the conversation without a cue", async () => {
  const h = await paired();
  await converse(h);
  const said = h.said.length;
  h.document.visibilityState = "hidden";
  await h.fire("visibilitychange");
  assert.equal(h.state(), ended(REASON.HIDDEN));
  assert.equal(h.said.length, said);
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.rec().aborts, 1);
});

test("the page being frozen or unloaded ends the conversation silently, while still visible", async () => {
  for (const event of ["freeze", "window:pagehide"]) {
    const h = await paired();
    await converse(h);
    assert.equal(h.document.visibilityState, "visible");
    const said = h.said.length;
    await h.fire(event);
    assert.equal(h.state(), ended(REASON.EXITED), event);
    assert.equal(h.said.length, said, event + " spoke a closing cue");
    assert.deepEqual(releases(h), ["g1"], event);
  }
});

test("the page being frozen or unloaded cuts a closing cue short and lets the grant go", async () => {
  for (const event of ["freeze", "window:pagehide"]) {
    const h = await paired();
    await converse(h);
    h.$("conv-end").click();
    await settle();
    assert.equal(h.utt().text, ended(REASON.END_TAPPED));
    assert.deepEqual(releases(h), []);
    const cancels = h.synth.cancels;
    await h.fire(event);
    assert.ok(h.synth.cancels > cancels, event + " left the closing cue playing");
    assert.deepEqual(releases(h), ["g1"], event);
  }
});

test("a dropped token ends the conversation and speaks nothing", async () => {
  const h = await paired();
  await converse(h);
  const said = h.said.length;
  await h.answer(POLL, 401, { error: "unauthorized" });
  assert.equal(h.state(), ended(REASON.UNPAIRED));
  assert.equal(h.said.length, said);
  assert.equal(h.$("pairing").hidden, false);
  assert.deepEqual(releases(h), ["g1"]);
});

test("a dropped token leaves no reply, draft or label on screen, only the end reason", async () => {
  for (const ending of ["during the conversation", "after it ended"]) {
    const h = await paired();
    await converse(h);
    await voiceSend(h, "check the logs");
    await h.answer(PATH.SEND, 200, { request_id: "r1", state: "handed_off" });
    h.utt().fire("end");
    await settle();
    await h.answer(POLL, 200, snap({ requests: [voiceRq("r1", "answered", "All good.")] }));
    assert.equal(h.$("conv-reply").textContent, "full", ending);
    let reason = REASON.UNPAIRED;
    if (ending === "after it ended") {
      h.$("conv-end").click();
      await settle();
      reason = REASON.END_TAPPED;
    }
    await h.answer(POLL, 401, { error: "unauthorized" });
    assert.equal(h.$("pairing").hidden, false, ending);
    assert.equal(h.$("conv-overlay").hidden, false, ending);
    assert.equal(h.state(), ended(reason), ending);
    for (const id of ["conv-reply", "conv-draft", "conv-interim", "conv-dest", "replies", "detail-title", "draft"]) {
      assert.equal(h.$(id).textContent + (h.$(id).value || ""), "", ending + ": " + id);
    }
    assert.equal(h.$("conv-reply-area").hidden, true, ending);
  }
});

test("the tap flow's controls are disabled while a conversation holds the voice, and return after", async () => {
  const s = () => snap({ requests: [rq("old", "answered", "Earlier reply.")] });
  const h = await paired(s());
  // Send lives in the review panel, which a Start closes and Review can't reopen.
  const controls = () => [h.$("dictate"), h.$("review-btn"), h.$("test-btn"), h.$("draft"), ...h.plays()];
  assert.equal(h.plays().length, 1);
  assert.ok(controls().slice(0, 4).every((c) => !c.disabled));
  await converse(h, s());
  assert.ok(controls().every((c) => c.disabled));
  assert.equal(h.$("review").hidden, true);
  h.$("conv-end").click();
  await settle();
  assert.equal(h.state(), ended(REASON.END_TAPPED));
  assert.equal(h.$("conv-end").textContent, "Close");
  // The closing cue still holds the voice.
  assert.ok(controls().every((c) => c.disabled));
  h.utt().fire("end");
  await settle();
  assert.ok(controls().slice(1, 4).every((c) => !c.disabled));
  assert.ok(h.plays().every((c) => !c.disabled));
  // Dictate opens a mic, so it also waits out the guard after the cue.
  assert.equal(h.$("dictate").disabled, true);
  await h.advance(L.LIMITS.GUARD_MS);
  assert.equal(h.$("dictate").disabled, false);
  h.$("conv-end").click();
  assert.equal(h.$("conv-overlay").hidden, true);
});

test("a reply that arrives during a conversation is spoken once, by the controller", async () => {
  const h = await paired();
  await converse(h);
  h.rec().hear("check the logs");
  h.rec().hear("send it");
  await settle();
  await h.answer(PATH.TICKET, 200, { ticket: "t1" });
  const post = await h.answer(PATH.SEND, 200, { request_id: "r1", state: "handed_off" });
  assert.equal(post.body.confirm, "voice");
  assert.equal(h.utt().text, CUE.SENT);
  h.utt().fire("end");
  await settle();
  const lock = { lock_id: "L1", request_id: "r1", since: 1, earlier_run: false };
  await h.answer(POLL, 200, snap({ targets: [target("s1", { lock })], requests: [voiceRq("r1", "handed_off")] }));
  await h.answer(POLL, 200, snap({ requests: [voiceRq("r1", "answered", "All good.")] }));
  assert.equal(h.utt().text, "All good.");
  assert.equal(h.state(), "Reading the reply. The mic is closed.");
  h.$("conv-end").click();
  await settle();
  h.utt().fire("end");
  await settle();
  await h.answer(POLL, 200, snap({ requests: [voiceRq("r1", "answered", "All good.")] }));
  assert.equal(h.said.filter((u) => u.text === "All good.").length, 1);
});

test("Start is disabled with its reason", async () => {
  const cases = [
    [snap({ voice_confirm_enabled: false }), BLOCKER.VOICE_OFF],
    [snap({ sends_enabled: false }), BLOCKER.SENDS_OFF],
    [snap({ requests: [rq("r9", "handed_off")] }), BLOCKER.BUSY],
    [snap({ targets: [target("s1", { lock: { lock_id: "L", request_id: "r9", since: 1, earlier_run: true } })] }), BLOCKER.BUSY]
  ];
  for (const [first, reason] of cases) {
    const h = await paired(first);
    assert.equal(h.$("conv-start").disabled, true, reason);
    assert.equal(h.$("conv-why").hidden, false);
    assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + reason + ".");
    h.$("conv-start").click();
    await settle();
    assert.equal(h.all(PATH.GRANT).length + h.pending(PATH.STATE).length, 0);
  }
  const ok = await paired();
  assert.equal(ok.$("conv-start").disabled, false);
  assert.equal(ok.$("conv-why").hidden, true);
});

test("Start is disabled without a recognizer", async () => {
  const h = await paired(snap(), { noRecognizer: true });
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + BLOCKER.NO_RECOGNIZER + ".");
});

test("Start stays disabled while sound is uncertain after a cut-off utterance", async () => {
  const h = await paired();
  await converse(h);
  h.rec().hear("open the hatch");
  h.rec().hear("read it back");
  await settle();
  assert.equal(h.utt().text, "open the hatch");
  h.$("conv-end").click();
  await settle();
  // Cut off mid read-back: no closing cue, and Start waits out twice the estimate.
  assert.equal(h.utt().text, "open the hatch");
  h.$("conv-end").click();
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + BLOCKER.NOT_QUIET + ".");
  await h.advance(L.estimateMs("open the hatch") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(h.$("conv-start").disabled, false);
});

test("the page's own Play utterance is tracked toward Start's quiet rule", async () => {
  const h = await paired(snap({ requests: [rq("old", "answered", "Earlier reply.")] }));
  h.plays()[0].click();
  await settle();
  assert.equal(h.utt().text, "Earlier reply.");
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + BLOCKER.NOT_QUIET + ".");
  h.utt().fire("end");
  await settle();
  assert.equal(h.$("conv-start").disabled, false);
});

test("a reply that removes the lock under an open Clear confirmation is spoken and polling goes on", async () => {
  const lock = { lock_id: "L1", request_id: "r1", since: 1, earlier_run: false };
  const h = await paired(snap({ targets: [target("s1", { lock })], requests: [rq("r1", "handed_off")] }));
  h.$("clear-btn").click();
  assert.equal(h.$("clear-confirm").hidden, false);
  // The reply's own speech asks for a render before the new snapshot is fully applied.
  await h.answer(POLL, 200, snap({ requests: [rq("r1", "answered", "All good.")] }));
  assert.equal(h.utt().text, "All good.");
  assert.equal(h.$("clear-confirm").hidden, true);
  assert.equal(h.$("clear-area").hidden, true);
  assert.equal(h.pending(POLL).length, 1, "polling stopped");
  assert.equal(h.$("conn").textContent, "Connected");
});

test("a fault while applying one snapshot is shown, still reaches the conversation, and polling goes on", async () => {
  const h = await paired();
  await converse(h);
  const title = h.$("detail-title");
  let faults = 1;
  Object.defineProperty(title, "textContent", { configurable: true, get: () => "", set: () => {
    if (faults-- > 0) throw new Error("render fault");
  } });
  // Sends turned off: the conversation must still see this snapshot and end.
  await h.answer(POLL, 200, snap({ sends_enabled: false }));
  assert.equal(h.state(), ended(REASON.SENDS_OFF));
  assert.equal(h.$("conn").getAttribute("data-state"), "fault");
  assert.equal(h.pending(POLL).length, 1, "polling stopped");
  await h.answer(POLL, 200, snap());
  assert.equal(h.$("conn").textContent, "Connected");
  assert.equal(h.pending(POLL).length, 1);
});

const dictateWhy = (reason) => "Dictate is unavailable: " + reason + ".";

test("Dictate stays shut while a cut-off conversation utterance may still sound", async () => {
  const h = await paired();
  await converse(h);
  h.rec().hear("open the hatch");
  h.rec().hear("read it back");
  await settle();
  h.$("conv-end").click();
  await settle();
  h.$("conv-end").click();
  assert.equal(h.$("conv-overlay").hidden, true);
  const recs = h.recs.length, tickets = h.all(PATH.TICKET).length;
  assert.equal(h.$("dictate").disabled, true);
  assert.equal(h.$("dictate-why").hidden, false);
  assert.equal(h.$("dictate-why").textContent, dictateWhy(BLOCKER.NOT_QUIET));
  h.$("dictate").dispatch("click");
  await settle();
  assert.equal(h.recs.length, recs, "a recognizer opened while sound was uncertain");
  assert.equal(h.all(PATH.TICKET).length, tickets);
  await h.advance(L.estimateMs("open the hatch") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(h.$("dictate").disabled, false);
  assert.equal(h.$("dictate-why").hidden, true);
  h.$("dictate").click();
  await settle();
  assert.equal(h.recs.length, recs + 1);
  assert.equal(h.rec().starts, 1);
});

test("Dictate waits for Play's end and the guard after it", async () => {
  const h = await paired(snap({ requests: [rq("old", "answered", "Earlier reply.")] }));
  h.plays()[0].click();
  await settle();
  assert.equal(h.$("dictate").disabled, true);
  assert.equal(h.$("dictate-why").textContent, dictateWhy(BLOCKER.NOT_QUIET));
  h.utt().fire("end");
  await settle();
  assert.equal(h.$("dictate").disabled, true, "the guard after the end has not passed");
  await h.advance(L.LIMITS.GUARD_MS);
  assert.equal(h.$("dictate").disabled, false);
  assert.equal(h.$("dictate-why").hidden, true);
});

test("a Play replaced just before its deadline: the old watchdog never cuts the new one", async () => {
  const long = "A much longer reply that keeps going for quite a while after it starts.";
  const h = await paired(snap({ requests: [rq("a", "answered", "Short."), rq("b", "answered", long)] }));
  slowCancel(h);
  playFor(h, "a").click();
  await settle();
  const first = h.utt();
  await h.advance(L.estimateMs("Short.") * L.SPEECH_DEADLINE_FACTOR - 50);
  playFor(h, "b").click();
  await settle();
  const second = h.utt();
  assert.equal(second.text, long);
  const cancels = h.synth.cancels;
  // Past the first Play's deadline, its cancel's error not yet in.
  await h.advance(100);
  assert.equal(h.synth.cancels, cancels, "the old Play's watchdog cancelled the new one");
  first.fire("error", { error: "interrupted" });
  await settle();
  assert.equal(h.synth.cancels, cancels, "a late error cancelled newer speech");
  assert.equal(second.over, undefined);
  // The new Play keeps its own deadline.
  await h.advance(L.estimateMs(long) * L.SPEECH_DEADLINE_FACTOR - 101);
  assert.equal(h.synth.cancels, cancels);
  await h.advance(1);
  assert.equal(h.synth.cancels, cancels + 1);
});

test("a late end of written-off speech stops one-shot dictation and drops its words", async () => {
  const h = await paired(snap({ requests: [rq("a", "answered", "Short.")] }));
  slowCancel(h);
  playFor(h, "a").click();
  await settle();
  const play = h.utt();
  const deadline = L.estimateMs("Short.") * L.SPEECH_DEADLINE_FACTOR;
  await h.advance(deadline);
  assert.equal(h.synth.cancels, 2, "the watchdog did not cancel the Play");
  await h.advance(deadline);
  h.$("draft").value = "Typed first.";
  h.$("draft").dispatch("input");
  await h.answer(PATH.TICKET, 200, { ticket: "tap-ticket" });
  h.$("dictate").click();
  await settle();
  const dictation = h.rec();
  assert.equal(dictation.starts, 1);
  dictation.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: "and then" }], { isFinal: false })] });
  dictation.hear("open the hatch");
  assert.equal(h.$("draft").value, "Typed first. open the hatch");
  play.fire("end");
  await settle();
  assert.equal(dictation.aborts, 1);
  assert.equal(h.$("draft").value, "Typed first.");
  assert.equal(h.$("interim").textContent, "");
  assert.equal(h.$("dictate").textContent, "Dictate");
  assert.equal(h.$("compose-note").textContent, "Dictation stopped: " + REASON.ECHO + ". Its words were discarded.");
  assert.equal(h.$("dictate").disabled, true, "the desk may still be speaking");
});

test("the hub's handoff refusal for a changed target ends the conversation silently", async () => {
  const h = await paired();
  await converse(h);
  await voiceSend(h, "check the logs");
  const said = h.said.length;
  await h.answer(PATH.SEND, 409, { error: "target changed; not sent" });
  assert.equal(h.state(), ended(L.withDetail(REASON.NOT_SENT, "target changed; not sent")));
  assert.equal(h.said.length, said, "a page that lost the sound slot spoke");
  assert.deepEqual(releases(h), ["g1"]);
});

test("losing the hub with a voice send unanswered shows the cause and that delivery is unknown", async () => {
  const h = await paired();
  await converse(h);
  await voiceSend(h, "check the logs");
  const said = h.said.length;
  await h.advance(L.LIMITS.FRESH_MS + L.LIMITS.TICK_MS);
  assert.equal(h.state(), ended(REASON.HUB_LOST + "; " + REASON.UNKNOWN));
  assert.equal(h.said.length, said);
  assert.equal(h.all(PATH.SEND).length, 1);
});

test("a stopped conversation's late reply is never voiced by the tap flow, and Play still works", async () => {
  const h = await paired();
  await converse(h);
  await voiceSend(h, "check the logs");
  await h.answer(PATH.SEND, 200, { request_id: "r1", state: "handed_off" });
  h.utt().fire("end");
  await settle();
  h.$("conv-end").click();
  await settle();
  h.utt().fire("end");
  await settle();
  h.$("conv-end").click();
  assert.equal(h.$("conv-overlay").hidden, true);
  const said = h.said.length;
  await h.answer(POLL, 200, snap({ requests: [voiceRq("r1", "answered", "All good.")] }));
  assert.equal(h.said.length, said, "the tap flow spoke a conversation's reply");
  const play = h.plays()[0];
  assert.equal(play.disabled, false);
  play.click();
  await settle();
  assert.equal(h.utt().text, "All good.");
});

test("a voice send whose POST never answered is not voiced by the tap flow either", async () => {
  const h = await paired();
  await converse(h);
  await voiceSend(h, "check the logs");
  h.$("conv-end").click();
  await settle();
  h.$("conv-end").click();
  const said = h.said.length;
  await h.answer(POLL, 200, snap({ requests: [voiceRq("r1", "answered", "All good.")] }));
  assert.equal(h.said.length, said, "the tap flow spoke a conversation's reply");
});

test("a tapped send's reply is still spoken on arrival", async () => {
  const h = await paired(snap({ requests: [rq("r0", "handed_off")] }));
  await h.answer(POLL, 200, snap({ requests: [rq("r0", "answered", "Done.")] }));
  assert.equal(h.utt().text, "Done.");
});

test("Start stays refused while a tapped send is in flight, whatever target is selected", async () => {
  const two = () => snap({ targets: [target("s1"), target("s2")] });
  const h = await paired(two());
  h.target("s1").click();
  h.$("draft").value = "hello";
  h.$("draft").dispatch("input");
  await h.answer(PATH.TICKET, 200, { ticket: "tap-ticket" });
  h.$("review-btn").click();
  h.$("send-btn").click();
  await settle();
  assert.equal(h.pending(PATH.SEND).length, 1);
  h.target("s2").click();
  await settle();
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + BLOCKER.TAP_SEND + ".");
  await h.answer(PATH.SEND, 200, { request_id: "r1", state: "handed_off" });
  assert.equal(h.$("conv-start").disabled, false);
});

const why = (reason) => "Start conversation is unavailable: " + reason + ".";

test("Start's reason names a conversation in progress, and an abandoned grant request being let go", async () => {
  const h = await paired();
  await converse(h);
  assert.equal(h.$("conv-why").textContent, why(BLOCKER.IN_PROGRESS));
  h.$("conv-end").click();
  await settle();
  h.utt().fire("end");
  await settle();
  h.$("conv-end").click();
  await h.advance(L.LIMITS.GUARD_MS);
  assert.equal(h.$("conv-start").disabled, false);

  h.$("conv-start").click();
  await settle();
  await h.answer(PATH.STATE, 200, snap());
  assert.equal(h.pending(PATH.GRANT).length, 1);
  await h.advance(L.LIMITS.STEP_MS);
  assert.equal(h.state(), ended(REASON.HUB_UNREACHABLE));
  h.$("conv-end").click();
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("conv-why").textContent, why(BLOCKER.LETTING_GO));
  await h.answer(PATH.GRANT, 200, { grant: "g2", expires_in: 15 });
  assert.deepEqual(releases(h), ["g1", "g2"]);
  // Start waits for that release to settle too.
  assert.equal(h.$("conv-why").textContent, why(BLOCKER.LETTING_GO));
  h.pending(PATH.RELEASE).find((c) => c.body.grant === "g2").answer(200, { ended: true });
  await settle();
  assert.equal(h.$("conv-start").disabled, false);
});

test("the overlay shows the reply's text, also when it has no spoken summary", async () => {
  const h = await paired();
  await converse(h);
  assert.equal(h.$("conv-reply-area").hidden, true);
  await voiceSend(h, "check the logs");
  await h.answer(PATH.SEND, 200, { request_id: "r1", state: "handed_off" });
  h.utt().fire("end");
  await settle();
  const reply = voiceRq("r1", "answered", "");
  reply.reply.text = "<b>Build</b> is green.";
  await h.answer(POLL, 200, snap({ requests: [reply] }));
  assert.equal(h.utt().text, CUE.NO_SPOKEN);
  assert.equal(h.$("conv-reply-area").hidden, false);
  assert.equal(h.$("conv-reply").textContent, "<b>Build</b> is green.");
  assert.equal(h.$("conv-reply").children.length, 0, "the reply went in as markup");
});

test("the overlay is modal: focus moves in, the page behind is inert, and both come back on Close", async () => {
  const h = await paired();
  assert.equal(h.$("page").inert, false);
  h.$("conv-start").focus();
  await converse(h);
  assert.equal(h.$("page").inert, true);
  assert.equal(h.document.activeElement, h.$("conv-end"));
  h.$("conv-end").click();
  await settle();
  h.utt().fire("end");
  await settle();
  await h.advance(L.LIMITS.GUARD_MS);
  assert.equal(h.$("page").inert, true, "the ended overlay is still up");
  h.$("conv-end").click();
  assert.equal(h.$("conv-overlay").hidden, true);
  assert.equal(h.$("page").inert, false);
  assert.equal(h.document.activeElement, h.$("conv-start"));
});

test("Close returns focus to Start even when the browser blurred it as it was disabled", async () => {
  const h = await paired();
  const start = h.$("conv-start");
  // Like a browser: disabling the focused control blurs it.
  let disabled = false;
  Object.defineProperty(start, "disabled", { configurable: true, get: () => disabled, set: (v) => {
    disabled = v;
    if (v && h.document.activeElement === start) h.document.activeElement = null;
  } });
  start.focus();
  await converse(h);
  assert.equal(h.document.activeElement, h.$("conv-end"));
  h.$("conv-end").click();
  await settle();
  h.utt().fire("end");
  await settle();
  await h.advance(L.LIMITS.GUARD_MS);
  assert.equal(start.disabled, false);
  h.$("conv-end").click();
  assert.equal(h.document.activeElement, start);
});

test("closing the overlay while Start is disabled returns focus to the session's heading", async () => {
  const h = await paired();
  h.$("conv-start").focus();
  await converse(h);
  h.rec().hear("open the hatch");
  h.rec().hear("read it back");
  await settle();
  h.$("conv-end").click();
  await settle();
  h.$("conv-end").click();
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("page").inert, false);
  assert.equal(h.document.activeElement, h.$("detail-title"));
});

test("a render fault during the page's own speech is shown and the speech still plays", async () => {
  const h = await paired(snap({ requests: [rq("old", "answered", "Earlier reply.")] }));
  const title = h.$("detail-title");
  let faults = 1;
  Object.defineProperty(title, "textContent", { configurable: true, get: () => "", set: () => {
    if (faults-- > 0) throw new Error("render fault");
  } });
  h.plays()[0].click();
  await settle();
  assert.equal(h.utt().text, "Earlier reply.");
  assert.equal(h.$("conn").getAttribute("data-state"), "fault");
});
