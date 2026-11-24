// Conversation mode's pure state machine (docs/voice-desk.md, "Conversation
// mode"): reduce(c, event) -> {c, fx}. No DOM, clock or I/O: every event
// carries `now` (a monotonic ms clock) and `quietAt` (when this page's most
// recent utterance ended, errored or was cancelled), and every effect is a
// plain object that the controller (conversation.js) carries out. The
// controller gives each recognizer, utterance, timer and fetch its own id and
// passes on only callbacks from the current one; this reducer then checks
// that the phase allows what the event would do. Tested through `reduce` by
// tests/js/conversation_logic.test.js (run by tests/test_desk_conversation_js.py).
(function (root) {
  "use strict";

  var Rule = root.DeskTextRule || require("./text_rule.js");

  var V = root.DeskConversationVocabulary || require("./conversation_vocabulary.js");
  var PHASE = V.PHASE, CONFIRM = V.CONFIRM, REQUEST_STATE = V.REQUEST_STATE, REGISTRATION = V.REGISTRATION;
  var LIMITS = V.LIMITS, GRANT_ID = V.GRANT_ID, SPEECH_DEADLINE_FACTOR = V.SPEECH_DEADLINE_FACTOR;
  var TIMER = V.TIMER, SAY = V.SAY, FX = V.FX, EV = V.EV, COMMAND = V.COMMAND, CUE = V.CUE;
  var REASON = V.REASON, BLOCKER = V.BLOCKER, withDetail = V.withDetail;

  // Only an isolated phrase is a command; anything else is draft text.
  var PHRASES = Object.freeze({
    "send it": COMMAND.SEND, "scratch that": COMMAND.SCRATCH,
    "read it back": COMMAND.READBACK, "stop listening": COMMAND.STOP
  });

  // The ends the page decides by itself, the only ones its closing cue may
  // follow (the controller adds that the grant is held and fresh, the page
  // visible, the voice there and nothing cut off). Every other end is
  // silent, whatever brought it: what the hub answers, shows or fails to
  // answer can only stop this page speaking, never permit it, since another
  // page may own the sound slot by then. A reason added to REASON is silent
  // until it is added here.
  var SPOKEN_ENDS = Object.freeze([
    REASON.STOPPED, REASON.END_TAPPED, REASON.IDLE, REASON.TURN, REASON.REPLY_TIMEOUT,
    REASON.REC_FAILED, REASON.REC_HUNG, REASON.REC_LOOP
  ]);

  var OUTSTANDING = {};
  OUTSTANDING[REQUEST_STATE.PENDING] = true;
  OUTSTANDING[REQUEST_STATE.HANDED_OFF] = true;
  var USABLE = {};
  USABLE[REGISTRATION.REPLIED] = true;
  USABLE[REGISTRATION.REGISTERED] = true;
  // The POST's answer states that mean the hub took the request.
  var ACCEPTED = {};
  ACCEPTED[REQUEST_STATE.HANDED_OFF] = true;
  ACCEPTED[REQUEST_STATE.ANSWERED] = true;

  function initial() {
    return {
      phase: PHASE.OFF, dest: null, label: "", draft: "", frozenText: null,
      // Which start this is, counted per page: a forget() replaces the dest, not the conversation.
      serial: 0,
      // Request ids the start's snapshot held for the target (history), and
      // the ids this conversation's sends returned.
      history: {}, historyTaken: false, own: {},
      // Whether this turn's send POST was issued; while it is unresolved,
      // the request ids and lock owners seen on the target, judged when it settles.
      posted: false, evidence: null,
      current: null, seenCurrent: false, sentCueLive: false, pendingReply: null,
      // Which mic opening the pending guard timer is for: "turn" or "resume".
      guardFor: null,
      quickEnds: 0, listenAt: null, freshUntil: 0, snap: null,
      // The drafting deadlines as absolute times; their timers may fire late.
      turnAt: null, idleAt: null,
      reason: null,
      // After an end that left a send POST unanswered: the end's own cause,
      // and whether the POST's answer may still settle what the reason says.
      cause: null, deliveryOpen: false
    };
  }

  function active(c) { return c.phase !== PHASE.OFF && c.phase !== PHASE.ENDED; }
  function fresh(c, now) { return c.freshUntil > 0 && now <= c.freshUntil; }

  // The deadline after a beat started at `startedAt` succeeded at `now`: 0
  // when the old deadline had already passed, since nothing revives a grant
  // the page gave up on, and never earlier than the old one.
  function renew(freshUntil, startedAt, now) {
    if (!(freshUntil > 0 && now <= freshUntil)) return 0;
    return Math.max(freshUntil, startedAt + LIMITS.FRESH_MS);
  }

  function utf8Bytes(text) { return new TextEncoder().encode(text).length; }

  // "Send it." / " SEND IT " -> COMMAND.SEND; anything else -> null.
  function command(text) {
    var norm = String(text).toLowerCase().replace(/\p{P}/gu, "").replace(/\s+/g, " ").trim();
    return Object.prototype.hasOwnProperty.call(PHRASES, norm) ? PHRASES[norm] : null;
  }

  // Appends a final result word for word. A result that would take the
  // draft past the limit clears it all, so no prefix can be sent unnoticed.
  function appendDraft(draft, text) {
    var piece = String(text).trim();
    if (!piece) return { draft: draft, overflow: false };
    var next = draft ? draft + " " + piece : piece;
    if (utf8Bytes(next) > LIMITS.MAX_DRAFT_BYTES) return { draft: "", overflow: true };
    return { draft: next, overflow: false };
  }

  // The hub's text rule (text_rule.js mirrors text_rule.py) plus the voice
  // limit; null when sendable.
  function draftProblem(draft) {
    return Rule.checkText(draft) || (utf8Bytes(draft) > LIMITS.MAX_DRAFT_BYTES ? "too long for a voice send" : null);
  }

  // About 150 words a minute plus a second of start-up. The spike measured
  // about 190 words a minute, so twice this is roughly 2.5 times real speech.
  function estimateMs(text) {
    var words = String(text).trim().split(/\s+/).filter(Boolean).length;
    return 1000 + words * 400;
  }

  function findTarget(snap, id) {
    var targets = (snap && snap.targets) || [];
    for (var i = 0; i < targets.length; i++) if (targets[i].session_id === id) return targets[i];
    return null;
  }

  function requestsFor(snap, id) {
    return ((snap && snap.requests) || []).filter(function (rq) { return rq.session_id === id; });
  }

  // Ends that hold in every phase: the epoch, the switches and the target
  // (absent, at another generation, unenrolled or unregistered), as the
  // first REASON that holds for `dest` in snapshot `snap`, or null when it
  // is usable. The controller asks the same of every poll while the
  // closing cue plays.
  function targetProblem(dest, snap) {
    if (snap.epoch !== dest.epoch) return REASON.EPOCH;
    if (snap.sends_enabled !== true) return REASON.SENDS_OFF;
    if (snap.voice_confirm_enabled !== true) return REASON.VOICE_OFF;
    if (snap.lock_store_error) return REASON.LOCK_STORE;
    var t = findTarget(snap, dest.session_id);
    if (!t || t.generation !== dest.generation || t.enrolled !== true || !USABLE[t.registration]) return REASON.TARGET;
    return null;
  }

  // Checked at every mic opening and before a final result is used: the
  // grant fresh, the tablet's voice there (`ev.voice`, read by the
  // controller for every event), the target available, and the turn's
  // absolute deadlines not passed, whether or not their timers have run.
  function admissionProblem(c, ev) {
    if (!fresh(c, ev.now)) return REASON.HUB_LOST;
    if (!ev.voice) return REASON.NO_VOICE;
    var why = targetProblem(c.dest, c.snap);
    if (why) return why;
    if (c.turnAt !== null && ev.now >= c.turnAt) return REASON.TURN;
    if (c.idleAt !== null && ev.now >= c.idleAt) return REASON.IDLE;
    return null;
  }

  // Foreign activity: a request on the target that is neither history from
  // the start nor one of ours, in any state (a foreign turn can start and
  // finish between two polls), or a lock not tied to our current request.
  function foreignRequest(c, id) { return !c.history[id] && !c.own[id]; }
  function foreignLock(c, requestId) { return c.current === null || requestId !== c.current; }

  function foreign(c, snap) {
    var t = findTarget(snap, c.dest.session_id);
    if (t && t.lock && foreignLock(c, t.lock.request_id)) return true;
    return requestsFor(snap, c.dest.session_id).some(function (rq) { return foreignRequest(c, rq.request_id); });
  }

  // While the send POST is unresolved our own request can't be told from a
  // foreign one yet, so everything seen is kept, not only the last poll.
  function gather(c, snap) {
    var t = findTarget(snap, c.dest.session_id);
    if (t && t.lock) c.evidence.locks[t.lock.request_id] = true;
    requestsFor(snap, c.dest.session_id).forEach(function (rq) { c.evidence.requests[rq.request_id] = true; });
  }

  // Judged once the POST returned our request id.
  function evidenceForeign(c) {
    return Object.keys(c.evidence.locks).some(function (id) { return foreignLock(c, id); }) ||
      Object.keys(c.evidence.requests).some(function (id) { return foreignRequest(c, id); });
  }

  // The first BLOCKER that holds for `targetId` in snapshot `snap`, or null
  // when Start may be offered. `page` = {recognizer, voice, tapSending,
  // canStart, blocker}: the tablet's own facts, the controller's canStart()
  // and, optionally, its blocker() for the reason Start is refused.
  function startBlocker(snap, targetId, page) {
    if (!snap) return BLOCKER.NO_STATE;
    if (snap.sends_enabled !== true) return BLOCKER.SENDS_OFF;
    if (snap.voice_confirm_enabled !== true) return BLOCKER.VOICE_OFF;
    if (snap.lock_store_error) return BLOCKER.LOCK_STORE;
    var t = findTarget(snap, targetId);
    if (!t || t.enrolled !== true || !USABLE[t.registration]) return BLOCKER.UNAVAILABLE;
    if (t.lock || requestsFor(snap, targetId).some(function (rq) { return OUTSTANDING[rq.state]; })) return BLOCKER.BUSY;
    if (!page.recognizer) return BLOCKER.NO_RECOGNIZER;
    if (!page.voice) return BLOCKER.NO_VOICE;
    if (page.tapSending) return BLOCKER.TAP_SEND;
    if (!page.canStart) return page.blocker || BLOCKER.NOT_QUIET;
    return null;
  }

  function timer(name, ms) { return { fx: FX.TIMER, name: name, ms: ms }; }
  function clearTimer(name) { return { fx: FX.CLEAR_TIMER, name: name }; }

  // The controller's END effect tears down every effect; the reducer only
  // forgets what the conversation held. `reason` is a REASON, shown with
  // its `detail` if any; the effect's `cue` is whether it is a SPOKEN_ENDS
  // reason, the only ends the controller may speak.
  // An end that abandons a send POST issued and never answered says so
  // after its cause, the prompt may have been delivered, until that POST's
  // own answer settles it (settleDelivery).
  function finish(c, fx, reason, detail) {
    var shown = withDetail(reason, detail);
    c.cause = shown;
    c.deliveryOpen = c.phase === PHASE.SENDING && c.posted && reason !== REASON.UNKNOWN;
    if (c.deliveryOpen) shown = shown + "; " + REASON.UNKNOWN;
    c.posted = false;
    c.phase = PHASE.ENDED;
    c.reason = shown;
    c.draft = "";
    c.frozenText = null;
    c.pendingReply = null;
    c.guardFor = null;
    fx.push({ fx: FX.END, reason: shown, cue: SPOKEN_ENDS.indexOf(reason) !== -1 });
  }

  // Every mic opening: only while admissionProblem finds nothing, and never
  // sooner than the guard after this page's last sound. Until the guard has
  // passed the phase stays where it is (starting, reading, speaking, or
  // listening between recognizers).
  function toListening(c, ev, fx, newTurn) {
    var why = admissionProblem(c, ev);
    if (why) return finish(c, fx, why);
    var wait = ev.quietAt + LIMITS.GUARD_MS - ev.now;
    if (wait > 0) {
      c.guardFor = newTurn ? "turn" : "resume";
      fx.push(timer(TIMER.GUARD, wait));
      return;
    }
    c.guardFor = null;
    c.phase = PHASE.LISTENING;
    if (newTurn) {
      c.draft = "";
      c.quickEnds = 0;
      // The turn's deadline runs from its first mic opening; nothing resets it.
      c.turnAt = ev.now + LIMITS.TURN_MS;
      c.idleAt = ev.now + LIMITS.IDLE_MS;
      fx.push(timer(TIMER.TURN, LIMITS.TURN_MS), timer(TIMER.IDLE, LIMITS.IDLE_MS));
    }
    c.listenAt = ev.now;
    fx.push({ fx: FX.LISTEN }, timer(TIMER.WATCHDOG, LIMITS.WATCHDOG_MS));
  }

  function closeMic(fx) { fx.push({ fx: FX.STOP_LISTEN }, clearTimer(TIMER.WATCHDOG), { fx: FX.INTERIM, text: "" }); }

  // A cue or read-back inside a turn: the mic is closed while it plays.
  function speakInTurn(c, fx, text, kind) {
    c.phase = PHASE.READING;
    closeMic(fx);
    fx.push({ fx: FX.SAY, text: text, kind: kind });
  }

  function speakReply(c, fx, reply) {
    c.phase = PHASE.SPEAKING;
    c.pendingReply = null;
    var text = typeof reply.spoken === "string" && reply.spoken.trim() ? reply.spoken : CUE.NO_SPOKEN;
    fx.push(clearTimer(TIMER.ACK), clearTimer(TIMER.REPLY), { fx: FX.SAY, text: text, kind: SAY.REPLY });
  }

  // Waiting follows the exact request id the POST returned.
  function follow(c, fx, snap) {
    var rq = requestsFor(snap, c.dest.session_id).filter(function (x) { return x.request_id === c.current; })[0];
    if (!rq) {
      // Absent before it was ever seen: the snapshot may predate the ack.
      if (c.seenCurrent) finish(c, fx, REASON.NO_REPLY);
      return;
    }
    if (!c.seenCurrent) { c.seenCurrent = true; fx.push(clearTimer(TIMER.ACK)); }
    if (OUTSTANDING[rq.state]) return;
    if (rq.state === REQUEST_STATE.ANSWERED && rq.reply) {
      if (!c.sentCueLive) return speakReply(c, fx, rq.reply);
      // Spoken once the "sent" cue's own end comes; the reply wait is over.
      c.pendingReply = rq.reply;
      fx.push(clearTimer(TIMER.REPLY));
      return;
    }
    finish(c, fx, rq.state === REQUEST_STATE.UNKNOWN ? REASON.UNKNOWN : REASON.NO_REPLY);
  }

  // The reply to the current request in the latest snapshot the conversation
  // took, or null. The overlay shows its full text, which matters most when
  // only "No spoken summary." was heard.
  function currentReply(c) {
    if (!c.current || !c.snap) return null;
    var rq = requestsFor(c.snap, c.dest.session_id).filter(function (x) { return x.request_id === c.current; })[0];
    return rq && rq.reply ? rq.reply : null;
  }

  // The token is gone: what the conversation kept from the hub (the
  // snapshot, and with it the reply; the request ids; the draft; the
  // session's label, also in its frozen dest) goes with it. The phase and
  // the end reason stay.
  function forget(c) {
    var next = copy(c);
    next.snap = null;
    next.current = null;
    next.pendingReply = null;
    next.draft = "";
    next.frozenText = null;
    next.history = {};
    next.own = {};
    next.evidence = null;
    next.label = "";
    if (next.dest) next.dest = Object.freeze(Object.assign({}, next.dest, { label: "" }));
    return next;
  }

  function newer(c, snap) {
    return !c.snap || snap.epoch !== c.snap.epoch || snap.version >= c.snap.version;
  }

  function onSnapshot(c, fx, ev) {
    var snap = ev.snapshot;
    // An older poll answering late never counts (a request "gone" from it
    // may only not have existed yet).
    if (!newer(c, snap)) return;
    c.snap = snap;
    recheck(c, fx);
  }

  function recheck(c, fx) {
    var why = targetProblem(c.dest, c.snap);
    if (why) return finish(c, fx, why);
    // Until the start's snapshot is taken there is no history to compare
    // with. Only while the POST itself is unresolved do the request and lock
    // checks wait, with what they saw kept; before it, the ticket fetch
    // included, foreign activity ends the conversation and nothing is posted.
    if (!c.historyTaken) return;
    if (c.phase === PHASE.SENDING && c.posted) return gather(c, c.snap);
    if (foreign(c, c.snap)) return finish(c, fx, REASON.FOREIGN);
    if (c.phase === PHASE.WAITING && !c.pendingReply) follow(c, fx, c.snap);
  }

  // The start's own snapshot: the target must be free, and what it already
  // holds for the target is history.
  function onStartSnapshot(c, fx, ev) {
    if (c.phase !== PHASE.STARTING || c.historyTaken) return;
    var snap = ev.snapshot;
    if (newer(c, snap)) c.snap = snap;
    var why = targetProblem(c.dest, snap);
    if (why) return finish(c, fx, why);
    var t = findTarget(snap, c.dest.session_id);
    var mine = requestsFor(snap, c.dest.session_id);
    if (t.lock || mine.some(function (rq) { return OUTSTANDING[rq.state]; })) return finish(c, fx, REASON.BUSY);
    mine.forEach(function (rq) { c.history[rq.request_id] = true; });
    c.historyTaken = true;
    // A newer poll already held may show a request the start's snapshot lacked.
    if (foreign(c, c.snap)) return finish(c, fx, REASON.BUSY);
    fx.push({ fx: FX.WAKE_LOCK });
  }

  function onWake(c, fx) {
    if (c.phase === PHASE.STARTING && c.historyTaken) fx.push({ fx: FX.GRANT });
  }

  // The last checks before the first mic opening, against the page as it is now.
  function onGrant(c, fx, ev) {
    if (c.phase !== PHASE.STARTING) return;
    c.freshUntil = ev.startedAt + LIMITS.FRESH_MS;
    if (!fresh(c, ev.now)) return finish(c, fx, REASON.HUB_LOST);
    fx.push(timer(TIMER.TICK, LIMITS.TICK_MS));
    var ctx = ev.ctx || {};
    if (ctx.selectedId !== c.dest.session_id) return finish(c, fx, REASON.RETARGET);
    if (!ctx.visible) return finish(c, fx, REASON.HIDDEN);
    if (!ev.voice) return finish(c, fx, REASON.NO_VOICE);
    var why = targetProblem(c.dest, c.snap);
    if (why) return finish(c, fx, why);
    if (foreign(c, c.snap)) return finish(c, fx, REASON.BUSY);
    toListening(c, ev, fx, true);
  }

  function onResult(c, fx, ev) {
    if (c.phase !== PHASE.LISTENING) return;
    if (!ev.final) { fx.push({ fx: FX.INTERIM, text: String(ev.text || "") }); return; }
    var why = admissionProblem(c, ev);
    if (why) return finish(c, fx, why);
    c.quickEnds = 0;
    c.idleAt = ev.now + LIMITS.IDLE_MS;
    fx.push({ fx: FX.INTERIM, text: "" }, timer(TIMER.IDLE, LIMITS.IDLE_MS));
    var cmd = command(ev.text);
    if (cmd === COMMAND.SEND) return onSendCommand(c, fx);
    if (cmd === COMMAND.SCRATCH) {
      c.draft = "";
      return speakInTurn(c, fx, CUE.CLEARED, SAY.CUE);
    }
    if (cmd === COMMAND.READBACK) {
      if (!c.draft) return speakInTurn(c, fx, CUE.EMPTY_DRAFT, SAY.CUE);
      return speakInTurn(c, fx, c.draft, SAY.READBACK);
    }
    if (cmd === COMMAND.STOP) return finish(c, fx, REASON.STOPPED);
    var next = appendDraft(c.draft, ev.text);
    c.draft = next.draft;
    if (next.overflow) speakInTurn(c, fx, CUE.OVERFLOW, SAY.CUE);
  }

  function onSendCommand(c, fx) {
    if (!c.draft) return speakInTurn(c, fx, CUE.NOTHING_TO_SEND, SAY.CUE);
    var problem = draftProblem(c.draft);
    if (problem) return speakInTurn(c, fx, CUE.CANT_SEND + ": " + problem + ".", SAY.CUE);
    // Out of `listening` in the same step, before any request.
    c.phase = PHASE.SENDING;
    c.frozenText = c.draft;
    c.posted = false;
    c.evidence = null;
    closeMic(fx);
    // The turn deadline is a drafting deadline; the reply wait is separate.
    c.turnAt = null;
    c.idleAt = null;
    fx.push(clearTimer(TIMER.IDLE), clearTimer(TIMER.TURN), { fx: FX.TICKET });
  }

  function onRecAudiostart(c, fx) {
    if (c.phase === PHASE.LISTENING) fx.push(clearTimer(TIMER.WATCHDOG));
  }

  // Recognition restarts on the recognizer's `end`: a silent session ends
  // with `nomatch` then `end` and no error.
  function onRecEnd(c, fx, ev) {
    if (c.phase !== PHASE.LISTENING) return;
    c.quickEnds = ev.now - c.listenAt < LIMITS.QUICK_END_MS ? c.quickEnds + 1 : 0;
    fx.push(clearTimer(TIMER.WATCHDOG), { fx: FX.INTERIM, text: "" });
    if (c.quickEnds >= LIMITS.QUICK_ENDS) return finish(c, fx, REASON.REC_LOOP);
    toListening(c, ev, fx, false);
  }

  // A no-speech error is not a failure: its `end` brings the restart.
  function onRecError(c, fx, ev) {
    if (c.phase !== PHASE.LISTENING || ev.error === "no-speech") return;
    finish(c, fx, REASON.REC_FAILED, ev.error);
  }

  function onRecStartFailed(c, fx) {
    if (c.phase === PHASE.LISTENING) finish(c, fx, REASON.REC_FAILED);
  }

  function onSendOk(c, fx, ev) {
    if (c.phase !== PHASE.SENDING) return;
    if (!ACCEPTED[ev.state] || typeof ev.request_id !== "string") return finish(c, fx, REASON.UNKNOWN);
    c.own[ev.request_id] = true;
    c.current = ev.request_id;
    c.posted = false;
    if (c.evidence && evidenceForeign(c)) return finish(c, fx, REASON.FOREIGN);
    c.evidence = null;
    c.seenCurrent = false;
    c.draft = "";
    c.frozenText = null;
    c.phase = PHASE.WAITING;
    c.sentCueLive = true;
    fx.push({ fx: FX.SAY, text: CUE.SENT, kind: SAY.SENT },
      timer(TIMER.ACK, LIMITS.ACK_WAIT_MS), timer(TIMER.REPLY, LIMITS.REPLY_WAIT_MS));
    // The answer may already be in the latest snapshot (it can beat the POST).
    if (c.snap) recheck(c, fx);
  }

  // Only the utterance each phase is waiting for moves it on.
  function onSayEnd(c, fx, ev) {
    if (ev.kind === SAY.SENT) {
      c.sentCueLive = false;
      if (c.phase === PHASE.WAITING && c.pendingReply) speakReply(c, fx, c.pendingReply);
      return;
    }
    if (c.phase === PHASE.READING && (ev.kind === SAY.CUE || ev.kind === SAY.READBACK)) toListening(c, ev, fx, false);
    else if (c.phase === PHASE.SPEAKING && ev.kind === SAY.REPLY) toListening(c, ev, fx, true);
  }

  var DRAFTING = {};
  DRAFTING[PHASE.LISTENING] = true;
  DRAFTING[PHASE.READING] = true;

  var TIMEOUTS = {};
  TIMEOUTS[TIMER.GUARD] = function (c, fx, ev) {
    if (c.guardFor) toListening(c, ev, fx, c.guardFor === "turn");
  };
  TIMEOUTS[TIMER.WATCHDOG] = function (c, fx) {
    if (c.phase === PHASE.LISTENING) finish(c, fx, REASON.REC_HUNG);
  };
  TIMEOUTS[TIMER.IDLE] = function (c, fx) {
    if (DRAFTING[c.phase]) finish(c, fx, REASON.IDLE);
  };
  TIMEOUTS[TIMER.TURN] = function (c, fx) {
    if (DRAFTING[c.phase]) finish(c, fx, REASON.TURN);
  };
  TIMEOUTS[TIMER.ACK] = function (c, fx) {
    if (c.phase === PHASE.WAITING && !c.seenCurrent) finish(c, fx, REASON.UNKNOWN);
  };
  TIMEOUTS[TIMER.REPLY] = function (c, fx) {
    if (c.phase === PHASE.WAITING && !c.pendingReply) finish(c, fx, REASON.REPLY_TIMEOUT);
  };

  function onTimeout(c, fx, ev) {
    var handler = TIMEOUTS[ev.name];
    if (handler) handler(c, fx, ev);
  }

  function onTick(c, fx, ev) {
    if (c.freshUntil === 0) return;
    if (!fresh(c, ev.now)) return finish(c, fx, REASON.HUB_LOST);
    fx.push(timer(TIMER.TICK, LIMITS.TICK_MS));
  }

  // Only the newest beat's success reaches here (the controller drops the
  // rest), and only while the grant is still fresh (reduce checks that).
  function onBeatOk(c, fx, ev) {
    if (c.freshUntil === 0) return;
    c.freshUntil = renew(c.freshUntil, ev.startedAt, ev.now);
  }

  var HANDLERS = {};
  HANDLERS[EV.END] = function (c, fx, ev) { finish(c, fx, ev.reason, ev.detail); };
  HANDLERS[EV.FAIL] = function (c, fx, ev) { if (c.phase === PHASE.STARTING) finish(c, fx, ev.reason, ev.detail); };
  HANDLERS[EV.SNAPSHOT_FETCHED] = onStartSnapshot;
  HANDLERS[EV.WAKE_OK] = onWake;
  HANDLERS[EV.GRANT_OK] = onGrant;
  HANDLERS[EV.SNAPSHOT] = onSnapshot;
  HANDLERS[EV.TICK] = onTick;
  HANDLERS[EV.BEAT_OK] = onBeatOk;
  HANDLERS[EV.RESULT] = onResult;
  HANDLERS[EV.REC_AUDIOSTART] = onRecAudiostart;
  HANDLERS[EV.REC_END] = onRecEnd;
  HANDLERS[EV.REC_ERROR] = onRecError;
  HANDLERS[EV.REC_START_FAILED] = onRecStartFailed;
  HANDLERS[EV.SAY_END] = onSayEnd;
  HANDLERS[EV.SAY_FAILED] = function (c, fx) { finish(c, fx, REASON.SPEECH); };
  HANDLERS[EV.TIMEOUT] = onTimeout;
  HANDLERS[EV.TICKET_OK] = function (c, fx, ev) {
    if (c.phase !== PHASE.SENDING || c.posted) return;
    c.posted = true;
    c.evidence = { requests: {}, locks: {} };
    fx.push({ fx: FX.SEND, ticket: ev.ticket, text: c.frozenText });
  };
  // A ticket or send that failed before handoff: `ev.reason`, the hub's
  // words or what became of the call, is shown after "not sent", and the
  // end is silent like every end the hub brings.
  function refused(c, fx, ev) {
    c.posted = false; // answered: nothing was handed off
    finish(c, fx, REASON.NOT_SENT, ev.reason);
  }

  HANDLERS[EV.TICKET_FAIL] = function (c, fx, ev) {
    if (c.phase === PHASE.SENDING) refused(c, fx, ev);
  };
  HANDLERS[EV.SEND_OK] = onSendOk;
  // A refusal before handoff ends the conversation; nothing is retried.
  HANDLERS[EV.SEND_REFUSED] = function (c, fx, ev) {
    if (c.phase === PHASE.SENDING) refused(c, fx, ev);
  };
  // An answer that leaves delivery open, or none at all: silent too.
  HANDLERS[EV.SEND_UNKNOWN] = function (c, fx) {
    if (c.phase === PHASE.SENDING) finish(c, fx, REASON.UNKNOWN);
  };

  var SEND_ANSWER = {};
  SEND_ANSWER[EV.SEND_OK] = true;
  SEND_ANSWER[EV.SEND_REFUSED] = true;
  SEND_ANSWER[EV.SEND_UNKNOWN] = true;

  // The answer to the send POST an end left open: what it establishes
  // replaces "delivery is unknown" after the end's cause. It changes only
  // the reason shown: the conversation stays ended and nothing is spoken.
  function settleDelivery(c, ev) {
    if (!c.deliveryOpen || !SEND_ANSWER[ev.type]) return;
    c.deliveryOpen = false;
    if (ev.type === EV.SEND_OK && ACCEPTED[ev.state] && typeof ev.request_id === "string") {
      c.reason = c.cause + "; " + REASON.HANDED_OFF;
    } else if (ev.type === EV.SEND_REFUSED) {
      c.reason = c.cause + "; " + withDetail(REASON.NOT_SENT, ev.reason);
    }
  }

  function copy(c) {
    var next = {};
    Object.keys(c).forEach(function (k) { next[k] = c[k]; });
    next.history = Object.assign({}, c.history);
    next.own = Object.assign({}, c.own);
    next.evidence = c.evidence && { requests: Object.assign({}, c.evidence.requests), locks: Object.assign({}, c.evidence.locks) };
    return next;
  }

  // The start event carries `dest`, the destination frozen at the tap:
  // {host, session_id, generation, label, epoch}.
  function reduce(prev, ev) {
    if (ev.type === EV.START) {
      if (active(prev)) return { c: prev, fx: [] }; // a second tap is ignored
      var c0 = initial();
      c0.phase = PHASE.STARTING;
      c0.serial = (prev.serial || 0) + 1;
      c0.dest = ev.dest;
      c0.label = ev.dest.label;
      return { c: c0, fx: [{ fx: FX.SNAPSHOT }] };
    }
    var handler = HANDLERS[ev.type];
    if (!active(prev) && prev.deliveryOpen && SEND_ANSWER[ev.type]) {
      var ended = copy(prev);
      settleDelivery(ended, ev);
      return { c: ended, fx: [] };
    }
    if (!active(prev) || !handler) return { c: prev, fx: [] };
    var c = copy(prev);
    var fx = [];
    // Freshness is an absolute deadline: once it has passed, no event acts
    // or renews, whether or not the 1 s tick has run yet. Only the page's
    // own end keeps its reason.
    if (ev.type !== EV.END && c.freshUntil > 0 && !fresh(c, ev.now)) {
      // The send's own answer, come too late to act on, still says what
      // became of the prompt.
      finish(c, fx, REASON.HUB_LOST);
      settleDelivery(c, ev);
      fx.forEach(function (f) { if (f.fx === FX.END) f.reason = c.reason; });
    } else {
      handler(c, fx, ev);
    }
    return { c: c, fx: fx };
  }

  var api = {
    PHASE: PHASE, CONFIRM: CONFIRM, REQUEST_STATE: REQUEST_STATE, REGISTRATION: REGISTRATION,
    LIMITS: LIMITS, TIMER: TIMER, SAY: SAY, FX: FX, EV: EV, COMMAND: COMMAND, CUE: CUE, REASON: REASON,
    BLOCKER: BLOCKER, SPEECH_DEADLINE_FACTOR: SPEECH_DEADLINE_FACTOR, SPOKEN_ENDS: SPOKEN_ENDS,
    GRANT_ID: GRANT_ID, targetProblem: targetProblem,
    initial: initial, reduce: reduce, active: active, fresh: fresh, renew: renew, withDetail: withDetail,
    startBlocker: startBlocker, currentReply: currentReply, forget: forget,
    command: command, appendDraft: appendDraft, draftProblem: draftProblem, estimateMs: estimateMs
  };
  root.DeskConversationLogic = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
