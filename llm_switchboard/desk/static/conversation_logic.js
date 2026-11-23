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

  // `listening` is the only phase with an open mic.
  var PHASE = Object.freeze({
    OFF: "off", STARTING: "starting", LISTENING: "listening", READING: "reading",
    SENDING: "sending", WAITING: "waiting", SPEAKING: "speaking", ENDED: "ended"
  });

  // Mirrors model.ConfirmMode, model.RequestState and model.RegistrationState
  // (tests/test_desk_conversation_js.py fails if they drift).
  var CONFIRM = Object.freeze({ TAP: "tap", VOICE: "voice" });
  var REQUEST_STATE = Object.freeze({
    PENDING: "pending", HANDED_OFF: "handed_off", ANSWERED: "answered", UNKNOWN: "unknown", EXPIRED: "expired"
  });
  var REGISTRATION = Object.freeze({ REPLIED: "replied", REGISTERED: "registered", NONE: "none", AMBIGUOUS: "ambiguous" });

  var LIMITS = Object.freeze({
    // After any desk sound ends, no recognizer starts sooner than this. The
    // tablet spike heard no leak with 1,500 ms; 500 ms is the plan's figure.
    // One constant for every mic opening.
    GUARD_MS: 500,
    // A start() that brings no audiostart in this time ends the conversation:
    // the restart gap measured 160 to 220 ms to audiostart, so this is a hang.
    WATCHDOG_MS: 5000,
    IDLE_MS: 3 * 60 * 1000,
    TURN_MS: 5 * 60 * 1000,
    ACK_WAIT_MS: 30 * 1000,
    REPLY_WAIT_MS: 10 * 60 * 1000,
    QUICK_END_MS: 1000,
    QUICK_ENDS: 3,
    BEAT_MS: 5000,
    BEAT_ABORT_MS: 4000,
    // The page gives up 10 s after the start of its last good beat, before
    // the hub's 15 s grant expiry (grants.GRANT_TTL_SECONDS), so the page
    // never acts on a grant the hub has already dropped.
    FRESH_MS: 10000,
    // How often the controller checks freshness between other events.
    TICK_MS: 1000,
    // A start step (the snapshot, the wake lock, the grant) or a ticket that
    // hasn't answered in this time fails; the send POST has the tap flow's deadline.
    STEP_MS: 10000,
    SEND_DEADLINE_MS: 20000,
    // Mirrors model.MAX_VOICE_TEXT_BYTES.
    MAX_DRAFT_BYTES: 1024
  });

  // An utterance gets twice the time estimated from its text before it counts
  // as failed; the same estimate sets the quiet period after a cut-off one.
  var SPEECH_DEADLINE_FACTOR = 2;

  // Every timer the conversation runs, the reducer's and the controller's,
  // so one name can't mean two timers.
  var TIMER = Object.freeze({
    GUARD: "guard", WATCHDOG: "watchdog", IDLE: "idle", TURN: "turn", ACK: "ack", REPLY: "reply",
    BEAT: "beat", BEAT_ABORT: "beat_abort", TICK: "tick", SPEECH: "speech", STEP: "step", SEND_ABORT: "send_abort"
  });

  // What an utterance is for; the end of the conversation's own last cue is
  // the controller's (END), never the reducer's.
  var SAY = Object.freeze({ CUE: "cue", READBACK: "readback", REPLY: "reply", SENT: "sent", END: "end" });

  var FX = Object.freeze({
    LISTEN: "listen", STOP_LISTEN: "stopListen", SAY: "say", INTERIM: "interim",
    TIMER: "timer", CLEAR_TIMER: "clearTimer", SNAPSHOT: "snapshot", WAKE_LOCK: "wakeLock",
    GRANT: "grant", TICKET: "ticket", SEND: "send", END: "end"
  });

  var EV = Object.freeze({
    START: "start", SNAPSHOT_FETCHED: "snapshot_fetched", WAKE_OK: "wake_ok", GRANT_OK: "grant_ok",
    FAIL: "fail", END: "end", SNAPSHOT: "snapshot", TICK: "tick", BEAT_OK: "beat_ok",
    RESULT: "result", REC_AUDIOSTART: "rec_audiostart", REC_END: "rec_end", REC_ERROR: "rec_error",
    REC_START_FAILED: "rec_start_failed", SAY_END: "say_end", SAY_FAILED: "say_failed",
    TIMEOUT: "timeout", TICKET_OK: "ticket_ok", TICKET_FAIL: "ticket_fail",
    SEND_OK: "send_ok", SEND_REFUSED: "send_refused", SEND_UNKNOWN: "send_unknown"
  });

  // Only an isolated phrase is a command; anything else is draft text.
  var COMMAND = Object.freeze({ SEND: "send", SCRATCH: "scratch", READBACK: "readback", STOP: "stop" });
  var PHRASES = Object.freeze({
    "send it": COMMAND.SEND, "scratch that": COMMAND.SCRATCH,
    "read it back": COMMAND.READBACK, "stop listening": COMMAND.STOP
  });

  var CUE = Object.freeze({
    SENT: "Sent.",
    NOTHING_TO_SEND: "Nothing to send.",
    CLEARED: "Cleared.",
    EMPTY_DRAFT: "The draft is empty.",
    OVERFLOW: "Too long. The draft was cleared.",
    CANT_SEND: "Can't send that",
    NO_SPOKEN: "No spoken summary.",
    ENDED: "Conversation ended"
  });

  // Why a conversation ended: spoken as "Conversation ended: <reason>." when
  // the page may still speak, else only shown. A reason with a detail is
  // `withDetail(REASON.X, detail)`.
  var REASON = Object.freeze({
    STOPPED: "you said stop listening",
    END_TAPPED: "End was tapped",
    HIDDEN: "the page was hidden",
    EXITED: "the page was closed or frozen",
    RETARGET: "another session was selected",
    UNPAIRED: "this tablet is no longer paired",
    WAKE: "the screen wake lock was released",
    NO_WAKE: "the screen can't be kept awake",
    NO_VOICE: "this tablet has no voice",
    IDLE: "nothing was heard for three minutes",
    TURN: "the turn took over five minutes",
    HUB_LOST: "the hub stopped answering",
    GRANT_LOST: "the hub ended the conversation",
    REFUSED: "the hub refused the conversation",
    HUB_UNREACHABLE: "the hub could not be reached",
    REC_FAILED: "speech recognition failed",
    REC_HUNG: "speech recognition did not start",
    REC_LOOP: "speech recognition keeps stopping",
    // A late end or error of the page's own speech: the mic may have heard it.
    ECHO: "the desk may have heard its own speech",
    SPEECH: "the voice failed",
    EPOCH: "the hub restarted",
    TARGET: "the session changed or went away",
    SENDS_OFF: "sends are off",
    VOICE_OFF: "voice confirm is off",
    LOCK_STORE: "the hub's lock store failed",
    BUSY: "the session is busy",
    FOREIGN: "another request reached the session",
    NOT_SENT: "not sent",
    UNKNOWN: "delivery is unknown; check the session",
    // Said after an end's cause once the send it left open was accepted.
    HANDED_OFF: "the prompt was handed off; check the session for its reply",
    NO_REPLY: "the request ended without a reply",
    REPLY_TIMEOUT: "no reply came in ten minutes"
  });

  // Why Start conversation is refused right now, shown under the control.
  // The reducer checks the hub's facts again once a conversation starts.
  var BLOCKER = Object.freeze({
    NO_STATE: "the hub has not answered yet",
    SENDS_OFF: REASON.SENDS_OFF,
    VOICE_OFF: REASON.VOICE_OFF,
    LOCK_STORE: REASON.LOCK_STORE,
    UNAVAILABLE: "the session is not available for sends",
    BUSY: REASON.BUSY,
    NO_RECOGNIZER: "this tablet has no speech recognition",
    NO_VOICE: REASON.NO_VOICE,
    TAP_SEND: "a tapped send is still in progress",
    NOT_QUIET: "the desk may still be speaking; wait until it is quiet",
    IN_PROGRESS: "a conversation is in progress",
    LETTING_GO: "the hub has not answered the last conversation's grant request; reload the page if it never does"
  });

  function withDetail(reason, detail) { return detail ? reason + ": " + detail : reason; }

  // The hub's refusals of a ticket or a send that leave this page's sound
  // slot intact (refusals.py, Ownership.INTACT): the hub gives each only
  // after checking, in the same call, the epoch, the target's one
  // registration at the frozen generation and, on a send, the grant. Any
  // other refusal, one this page has never seen included, may mean another
  // page owns the slot now. tests/test_desk_conversation_js.py checks this
  // equals the hub's set and drives the hub into each class.
  var OWNERSHIP_KEPT = Object.freeze(["busy", "ticket table full", "too many outstanding requests"]);

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
      reason: null, lost: false,
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

  function problem(reason, lost) { return { reason: reason, lost: !!lost }; }

  // Whether this page no longer owns the sound slot for `dest`: its grant
  // is gone, or bound to a registration that is. `proof` is the hub's
  // refusal of a ticket or a send, {refusal}, or a snapshot, {snapshot}. A
  // refusal keeps ownership only when it is in OWNERSHIP_KEPT; a refusal
  // with no words ("") is unknown, so it loses it too. A snapshot loses it
  // with another epoch (the grant died with that hub process), or the
  // target absent or at another generation (the hub drops a grant when its
  // registration goes). Neither, as when no answer came at all, proves
  // nothing: freshness decides. Decided apart from the reason shown: a
  // snapshot with voice confirm off and a new generation loses ownership
  // whatever it says first. A page that lost it never speaks again: a
  // successor may be listening.
  function ownershipLost(dest, proof) {
    if (typeof proof.refusal === "string") return OWNERSHIP_KEPT.indexOf(proof.refusal) === -1;
    var snap = proof.snapshot;
    if (!snap) return false;
    if (snap.epoch !== dest.epoch) return true;
    var t = findTarget(snap, dest.session_id);
    return !t || t.generation !== dest.generation;
  }

  // Ends that hold in every phase: the target, the epoch and the switches.
  // The reason shown is the first that holds; `lost` is ownershipLost's.
  function targetProblem(c, snap) {
    var lost = ownershipLost(c.dest, { snapshot: snap });
    if (snap.epoch !== c.dest.epoch) return problem(REASON.EPOCH, lost);
    if (snap.sends_enabled !== true) return problem(REASON.SENDS_OFF, lost);
    if (snap.voice_confirm_enabled !== true) return problem(REASON.VOICE_OFF, lost);
    if (snap.lock_store_error) return problem(REASON.LOCK_STORE, lost);
    var t = findTarget(snap, c.dest.session_id);
    if (lost || t.enrolled !== true || !USABLE[t.registration]) return problem(REASON.TARGET, lost);
    return null;
  }

  // Checked at every mic opening and before a final result is used: the
  // grant fresh, the tablet's voice there (`ev.voice`, read by the
  // controller for every event), the target available, and the turn's
  // absolute deadlines not passed, whether or not their timers have run.
  function admissionProblem(c, ev) {
    if (!fresh(c, ev.now)) return problem(REASON.HUB_LOST, true);
    if (!ev.voice) return problem(REASON.NO_VOICE);
    var why = targetProblem(c, c.snap);
    if (why) return why;
    if (c.turnAt !== null && ev.now >= c.turnAt) return problem(REASON.TURN);
    if (c.idleAt !== null && ev.now >= c.idleAt) return problem(REASON.IDLE);
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
  // forgets what the conversation held.
  // `lost`: the grant is gone; `silent`: the page is leaving. Either way the
  // controller speaks no closing cue.
  // An end that abandons a send POST issued and never answered says so
  // after its cause, the prompt may have been delivered, until that POST's
  // own answer settles it (settleDelivery).
  function finish(c, fx, reason, lost, silent) {
    c.cause = reason;
    c.deliveryOpen = c.phase === PHASE.SENDING && c.posted && reason !== REASON.UNKNOWN;
    if (c.deliveryOpen) reason = reason + "; " + REASON.UNKNOWN;
    c.posted = false;
    c.phase = PHASE.ENDED;
    c.reason = reason;
    c.lost = !!lost;
    c.draft = "";
    c.frozenText = null;
    c.pendingReply = null;
    c.guardFor = null;
    fx.push({ fx: FX.END, reason: reason, lost: !!lost, silent: !!silent });
  }

  // Every mic opening: only while admissionProblem finds nothing, and never
  // sooner than the guard after this page's last sound. Until the guard has
  // passed the phase stays where it is (starting, reading, speaking, or
  // listening between recognizers).
  function toListening(c, ev, fx, newTurn) {
    var why = admissionProblem(c, ev);
    if (why) return finish(c, fx, why.reason, why.lost);
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
  // session's label) goes with it. The phase and the end reason stay.
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
    var why = targetProblem(c, c.snap);
    if (why) return finish(c, fx, why.reason, why.lost);
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
    var why = targetProblem(c, snap);
    if (why) return finish(c, fx, why.reason, why.lost);
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
    if (!fresh(c, ev.now)) return finish(c, fx, REASON.HUB_LOST, true);
    fx.push(timer(TIMER.TICK, LIMITS.TICK_MS));
    var ctx = ev.ctx || {};
    if (ctx.selectedId !== c.dest.session_id) return finish(c, fx, REASON.RETARGET);
    if (!ctx.visible) return finish(c, fx, REASON.HIDDEN);
    if (!ev.voice) return finish(c, fx, REASON.NO_VOICE);
    var why = targetProblem(c, c.snap);
    if (why) return finish(c, fx, why.reason, why.lost);
    if (foreign(c, c.snap)) return finish(c, fx, REASON.BUSY);
    toListening(c, ev, fx, true);
  }

  function onResult(c, fx, ev) {
    if (c.phase !== PHASE.LISTENING) return;
    if (!ev.final) { fx.push({ fx: FX.INTERIM, text: String(ev.text || "") }); return; }
    var why = admissionProblem(c, ev);
    if (why) return finish(c, fx, why.reason, why.lost);
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
    finish(c, fx, withDetail(REASON.REC_FAILED, ev.error));
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
    if (!fresh(c, ev.now)) return finish(c, fx, REASON.HUB_LOST, true);
    fx.push(timer(TIMER.TICK, LIMITS.TICK_MS));
  }

  // Only the newest beat's success reaches here (the controller drops the
  // rest), and only while the grant is still fresh (reduce checks that).
  function onBeatOk(c, fx, ev) {
    if (c.freshUntil === 0) return;
    c.freshUntil = renew(c.freshUntil, ev.startedAt, ev.now);
  }

  var HANDLERS = {};
  HANDLERS[EV.END] = function (c, fx, ev) { finish(c, fx, ev.reason, ev.lost, ev.silent); };
  HANDLERS[EV.FAIL] = function (c, fx, ev) { if (c.phase === PHASE.STARTING) finish(c, fx, ev.reason, ev.lost); };
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
  // A ticket or send that failed before handoff. `ev.reason` is shown;
  // `ev.refusal`, the hub's own words when it answered with a refusal, is
  // judged by ownershipLost: unless it keeps ownership, the conversation
  // ends as lost, with no closing cue.
  function refused(c, fx, ev) {
    c.posted = false; // answered: nothing was handed off
    finish(c, fx, withDetail(REASON.NOT_SENT, ev.reason), ownershipLost(c.dest, { refusal: ev.refusal }));
  }

  HANDLERS[EV.TICKET_FAIL] = function (c, fx, ev) {
    if (c.phase === PHASE.SENDING) refused(c, fx, ev);
  };
  HANDLERS[EV.SEND_OK] = onSendOk;
  // A refusal before handoff ends the conversation; nothing is retried.
  HANDLERS[EV.SEND_REFUSED] = function (c, fx, ev) {
    if (c.phase === PHASE.SENDING) refused(c, fx, ev);
  };
  // An answer that leaves delivery open; one carrying a refusal is judged
  // like any other.
  HANDLERS[EV.SEND_UNKNOWN] = function (c, fx, ev) {
    if (c.phase === PHASE.SENDING) finish(c, fx, REASON.UNKNOWN, ownershipLost(c.dest, { refusal: ev.refusal }));
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
      finish(c, fx, REASON.HUB_LOST, true);
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
    BLOCKER: BLOCKER, SPEECH_DEADLINE_FACTOR: SPEECH_DEADLINE_FACTOR, OWNERSHIP_KEPT: OWNERSHIP_KEPT,
    ownershipLost: ownershipLost,
    initial: initial, reduce: reduce, active: active, fresh: fresh, renew: renew, withDetail: withDetail,
    startBlocker: startBlocker, currentReply: currentReply, forget: forget,
    command: command, appendDraft: appendDraft, draftProblem: draftProblem, estimateMs: estimateMs
  };
  root.DeskConversationLogic = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
