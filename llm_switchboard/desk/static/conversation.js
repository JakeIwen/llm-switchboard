// Conversation mode's controller (docs/voice-desk.md, "Conversation mode"):
// the one owner of every mic, sound and send effect while a conversation
// exists. conversation_logic.js decides; this file carries out its effects
// against the browser objects it is given, so it touches no DOM and node can
// drive it with fakes (tests/js/conversation.test.js).
//
// Effect identity: every recognizer, utterance, timer and fetch gets its own
// id when it is created, and a callback is honored only while its id is the
// current one of its kind. The id is cleared BEFORE anything is aborted or
// cancelled, and the handlers stay attached, so the `end` of an aborted
// recognizer or the answer of an abandoned fetch is seen and dropped.
//
// Public surface, all the page needs (app.js and the view):
//   DeskConversation.create({api, env, deps}) -> controller
//   DeskConversation.browserEnv(window) -> env from the page's own objects.
//     api(method, path, body, {signal, call}) -> Promise<{status, data}>,
//       rejecting when no response came (hub_api.js, through app.js, which
//       passes every status line to hubStatus(), every 200 whose body was
//       lost to bodyLost(), and drops the token on a 401).
//     env: {Recognition, synth, Utterance, setTimeout, clearTimeout, now,
//       wakeLock, AbortController}; `now` is a monotonic ms clock
//       (performance.now), `wakeLock` is navigator.wakeLock or null.
//     deps: {selectedId(), visible(), voiceAvailable(), onChange(view),
//       lateSound()}: read at the moments the plan checks them; onChange
//       after every change; lateSound() when a late end or error of the
//       page's own speech arrives, true when it stopped a one-shot dictation.
//   start(dest)       The Start tap. dest = {host, session_id, generation,
//                     label, epoch} frozen from the tapped control. False
//                     (and nothing happens) unless canStart().
//   canStart()        No conversation, no utterance of this page queued or
//                     playing, no uncertain sound, and no grant request the
//                     hub may still act on (given up on, or failed with no
//                     response; see abandonGrant): the Start control's
//                     enabled state.
//   blocker()         Why canStart() is false, as a BLOCKER (IN_PROGRESS,
//                     LETTING_GO, NOT_QUIET), or null; pass it to
//                     startBlocker as `page.blocker` for the view's reason.
//   snapshot(state, from) Every snapshot the page receives from the hub,
//                     whichever fetch brought it (`from`, a SNAPSHOT_FROM:
//                     the poll by default, the tap flow's refresh, an
//                     earlier pairing's answer), and null for a 200 with none
//                     (see snapshot below).
//   hubStatus(status, call) Every status line the hub sends, on any call
//                     (`call` from that call's opts), before its body: the
//                     gate (see hubStatus below).
//   bodyLost()        A 200 whose body never arrived, on any call, before
//                     the call's own handling: it silences (see bodyLost).
//   endTap()          The full-screen End conversation control; also stops
//                     the closing cue.
//   hidden()          The page was hidden (visibilitychange to hidden).
//   pageExit()        The page received `pagehide` or `freeze`, whatever
//                     visibilityState says: ends any conversation silently,
//                     cuts a closing cue, and still releases the grant.
//   targetSelected(id) The selection changed; another target ends it.
//   unpaired()        The token was dropped (401, revoke, expiry): ends any
//                     conversation silently, cuts a closing cue, and drops
//                     the snapshot, reply, draft and label it kept; only the
//                     end reason stays.
//   track(utterance)  Before the page speaks any utterance of its own (Play,
//                     the legacy reply speech), so its end, error or cancel
//                     counts toward the guard and Start's quiet rule. One not
//                     ended within twice its estimate, while it is still the
//                     one playing, is cancelled (the controller calls
//                     speechSynthesis.cancel()) and makes sound uncertain;
//                     its real end or error, whenever it comes, still
//                     restarts the guard.
//   stopPageSpeech()  Every cancel of the page's own speech (before Play or
//                     the legacy reply speech speaks again, on hiding, on a
//                     retarget): the stopped utterances count as cut off.
//                     Does nothing while holds().
//   soundClear()      For the page's one-shot Dictate, before it starts its
//                     own recognizer: true only when no utterance of this
//                     page is queued or playing, LIMITS.GUARD_MS has passed
//                     since the last real end, error or cancel of any of
//                     them, and no uncertain-sound period is running.
//                     deps.onChange fires when it turns true (the guard and
//                     the quiet period end on a timer). It says nothing about
//                     a conversation: Dictate also needs !holds().
//   view()            {phase, active, serial, dest, label, draft, interim, reason,
//                     speaking, canStart, soundClear, reply}: everything the
//                     view shows; `reply` is {text, spoken} of the current
//                     request once a snapshot holds it, else null.
//   holds()           A conversation is active or its closing cue plays: the
//                     page disables Dictate, Review, Send, Test, Play and its
//                     own reply speech, and leaves speech to the controller.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");
  var A = root.DeskConversationAnswers || require("./conversation_answers.js");
  var T = root.DeskSoundTracker || require("./sound_tracker.js");
  var F = root.DeskConversationFetches || require("./conversation_fetches.js");
  var TM = root.DeskConversationTimers || require("./conversation_timers.js");
  var R = root.DeskConversationRecognition || require("./conversation_recognition.js");
  var PHASE = L.PHASE, FX = L.FX, EV = L.EV, TIMER = L.TIMER, SAY = L.SAY;
  var REASON = L.REASON, CUE = L.CUE, LIMITS = L.LIMITS;
  var FETCH = F.FETCH, SPEC = F.SPEC;

  var PATH = Object.freeze({
    STATE: "/desk/api/state", GRANT: "/desk/api/conversation", BEAT: "/desk/api/conversation/beat",
    RELEASE: "/desk/api/conversation/end", TICKET: "/desk/api/ticket", SEND: "/desk/api/send"
  });

  // Where a snapshot the page received came from (snapshot()): the page's
  // poll, any other fetch of the current pairing, or a call made before the
  // token in use now was paired.
  var SNAPSHOT_FROM = Object.freeze({ POLL: "poll", FETCHED: "fetched", EARLIER_PAIRING: "earlier_pairing" });

  var UNREACHABLE = "the hub could not be reached";
  var detail = A.detail, sendEvent = A.sendEvent;

  function create(opts) {
    var api = opts.api, env = opts.env, deps = opts.deps;
    var c = L.initial();
    var queue = [];
    var draining = false;
    var seq = 0;
    var utt = null;          // {id, u, kind, estimate}
    var timers = {};         // TIMER name -> {id, handle}
    var clock = TM.create(env); // every timer below; each limit is the time it is due (heard)
    var fetches = F.create(nextId, env.AbortController, clock, bodyLost); // FETCH kind -> the one in flight
    var settle = fetches.settle, abandon = fetches.abandon;
    var wake = null;         // {id, sentinel}
    var grant = null;        // the grant id this conversation holds
    // {freshUntil} while the closing cue plays: the grant is still held, and
    // beaten and checked like a conversation's, until the cue ends or fails.
    var closing = null;
    // {id, epoch}: an abandoned grant request, or the release of the grant
    // it brought. Start waits for it, so it can't reach the hub after a
    // newer acquisition and replace that grant (the hub keeps the latest).
    var straggler = null;
    // How many times the token was dropped (unpaired): a call made before
    // the last drop answers for an earlier pairing.
    var pairing = 0;
    var sound = T.create({ env: env, onChange: notify, onLate: lateSound });
    var cutsAtStart = 0;     // sound.cuts() at the Start tap: more means this conversation cut one off
    var interim = "";
    var mic = R.create({ env: env, nextId: nextId, dispatch: dispatch,
      phase: function () { return c.phase; }, quietAt: function () { return sound.quietAt(); } });

    function nextId() { return ++seq; }

    // ---- dispatch ---------------------------------------------------------

    // Events raised while effects run (a synchronous callback, a failed
    // start()) wait their turn, so every effect list runs to the end first.
    function dispatch(ev) {
      queue.push(ev);
      if (draining) return;
      draining = true;
      try {
        while (queue.length) {
          var next = queue.shift();
          next.now = env.now();
          next.quietAt = sound.quietAt();
          next.voice = !!deps.voiceAvailable();
          var r = L.reduce(c, next);
          c = r.c;
          r.fx.forEach(run);
        }
      } finally {
        draining = false;
      }
      notify();
    }

    function notify() { if (deps.onChange) deps.onChange(view()); }

    var RUN = {};
    RUN[FX.LISTEN] = mic.start;
    RUN[FX.STOP_LISTEN] = mic.stop;
    RUN[FX.SAY] = function (fx) { speak(fx.text, fx.kind); };
    RUN[FX.INTERIM] = function (fx) { interim = fx.text; };
    RUN[FX.TIMER] = function (fx) { setTimer(fx.name, fx.ms); };
    RUN[FX.CLEAR_TIMER] = function (fx) { clearTimer(fx.name); };
    RUN[FX.SNAPSHOT] = fetchStartSnapshot;
    RUN[FX.WAKE_LOCK] = takeWakeLock;
    RUN[FX.GRANT] = requestGrant;
    RUN[FX.TICKET] = requestTicket;
    RUN[FX.SEND] = function (fx) { postSend(fx.ticket, fx.text); };
    RUN[FX.END] = function (fx) { teardown(fx.reason, fx.cue); };

    function run(fx) { RUN[fx.fx](fx); }

    // ---- timers -----------------------------------------------------------

    // Returns the clock's handle, the `bound` of a fetch this timer limits.
    function setTimer(name, ms) {
      clearTimer(name);
      var id = nextId();
      var handle = clock.setTimeout(function () {
        if (!timers[name] || timers[name].id !== id) return;
        delete timers[name];
        onTimer(name);
      }, ms);
      timers[name] = { id: id, handle: handle };
      return handle;
    }

    function clearTimer(name) {
      var t = timers[name];
      if (!t) return;
      delete timers[name];
      clock.clearTimeout(t.handle);
    }

    var ON_TIMER = {};
    ON_TIMER[TIMER.TICK] = function () {
      if (!closing) return dispatch({ type: EV.TICK });
      if (!L.fresh(closing, env.now())) return stopClosing();
      setTimer(TIMER.TICK, LIMITS.TICK_MS);
    };
    ON_TIMER[TIMER.BEAT] = beat;
    ON_TIMER[TIMER.BEAT_ABORT] = function () { abandon(FETCH.BEAT, true); };
    ON_TIMER[TIMER.STEP] = stepTimedOut;
    ON_TIMER[TIMER.SPEECH] = speechTimedOut;

    function onTimer(name) {
      if (ON_TIMER[name]) ON_TIMER[name]();
      else dispatch({ type: EV.TIMEOUT, name: name });
    }

    // ---- fetches ----------------------------------------------------------

    // The gate. Only a 200 can be a call's success; any other status, on
    // any call, whoever made it and whether or not its answer is still
    // wanted, cuts a closing cue (sound uncertain) and lets the grant go,
    // and ends a running conversation silently, with nothing spoken: the
    // reducer shows the status until this conversation's own call's
    // answer, if the status was one, settles what the end says (late). A
    // grant request answered so granted nothing, so Start stops waiting.
    // A call's own limits that passed before its status line apply first
    // (statusArrived); then a status that silences is applied before any
    // other overdue timer (heard). A 200's status line establishes nothing:
    // its body is what is used, or lost (bodyLost).
    function hubStatus(status, call) {
      fetches.statusArrived(call);
      if (status !== 200) clock.heard(true, function () { gate(status, call); });
    }

    function gate(status, call) {
      stopClosing();
      fetches.failed(call);
      var kind = fetches.kindOf(call);
      var send = kind === FETCH.SEND || fetches.isLateSend(call);
      var ending = active();
      if (ending && kind) fetches.lateCause(call);
      if (ending || send) dispatch({ type: EV.HUB_STATUS, status: status, own: ending && !!kind, send: send });
      if (straggler && straggler.id === call) forgetStraggler(call);
    }

    // The answer of a fetch an end left behind, as the event its call
    // would have dispatched: the one whose status ended the conversation
    // settles the end's cause once its body has arrived; the send POST the
    // end left open settles what became of the prompt. Others are dropped.
    function late(id, ev, r) {
      var role = fetches.takeLate(id);
      if (role.cause && r.data !== null && r.data !== undefined) return dispatch(Object.assign({ settles: true }, ev));
      if (role.send) dispatch(ev);
    }

    // An answer that silences the page, whichever call brought it and
    // whether or not its caller still wants it: a closing cue is cut, and a
    // running conversation, in any phase, ends silently with `reason`.
    function silence(reason) {
      if (closing) return stopClosing();
      if (active()) dispatch({ type: EV.END, reason: reason, silent: true });
    }

    // A 200 whose body read failed (hub_api.js), on any call the page makes,
    // current or abandoned, this pairing's or an earlier one's, before that
    // call's own handling: no body was received, so the 200 established
    // nothing, and it silences. A send's own answer, which follows, still
    // settles what became of the prompt (late): delivery unknown.
    function bodyLost() {
      clock.heard(true, function () { silence(REASON.UNUSABLE); });
    }

    // Every snapshot the page receives from the hub, whichever call brought
    // it (`from`, a SNAPSHOT_FROM): a 200 with none silences; a running
    // conversation's reducer takes one in version order, an older one or
    // one from an earlier pairing only for the target problem it shows,
    // never as state; and only one that shows the target usable leaves a
    // closing cue playing. The poll's alone can end the wait for an
    // abandoned grant request, by showing a new hub epoch. Only a usable
    // one waits for the timers already due (heard).
    function snapshot(state, from) {
      var usable = !!state && !(c.dest && L.targetProblem(c.dest, state));
      clock.heard(!usable, function () { took(state, from || SNAPSHOT_FROM.POLL); });
    }

    function took(state, from) {
      if (from === SNAPSHOT_FROM.POLL && state && straggler && state.epoch !== straggler.epoch) {
        forgetStraggler(straggler.id);
      }
      if (!state) return silence(REASON.UNUSABLE);
      if (active()) return dispatch({ type: EV.SNAPSHOT, snapshot: state, earlier: from === SNAPSHOT_FROM.EARLIER_PAIRING });
      var why = closing && L.targetProblem(c.dest, state);
      if (why) silence(why);
    }

    // A start step that never answers fails like one that refused; the grant
    // request is left open so a grant that still arrives is released.
    function stepTimedOut() {
      if (abandon(FETCH.SNAPSHOT, true) || abandonGrant()) {
        dispatch({ type: EV.FAIL, reason: REASON.HUB_UNREACHABLE });
      } else if (abandon(FETCH.WAKE, false)) {
        dispatch({ type: EV.FAIL, reason: REASON.NO_WAKE });
      } else if (abandon(FETCH.TICKET, true)) {
        dispatch({ type: EV.TICKET_FAIL, reason: UNREACHABLE });
      }
    }

    // A start step or the ticket: its fetch, limited by the step timer too,
    // which it names as its `bound` (conversation_fetches.js, lapse).
    function step(kind, call, on) {
      var bound = setTimer(TIMER.STEP, LIMITS.STEP_MS);
      return fetches.run(kind, Object.assign({ bound: bound }, SPEC[kind]), call, on);
    }

    function fetchStartSnapshot() {
      var askedIn = pairing;
      var id = step(FETCH.SNAPSHOT, function (o) { return api("GET", PATH.STATE, undefined, o); }, {
        fails: function (r) { return !(r.status === 200 && r.data) || !!L.targetProblem(c.dest, r.data); },
        answer: answered, none: function () {
          if (!settle(FETCH.SNAPSHOT, id)) return;
          clearTimer(TIMER.STEP);
          dispatch({ type: EV.FAIL, reason: REASON.HUB_UNREACHABLE });
        } });
      function answered(r) {
        var ev = r.status === 200 && r.data ? { type: EV.SNAPSHOT_FETCHED, snapshot: r.data }
          : { type: EV.FAIL, reason: REASON.HUB_UNREACHABLE };
        if (!settle(FETCH.SNAPSHOT, id)) {
          if (r.status === 200) {
            snapshot(r.data, askedIn === pairing ? SNAPSHOT_FROM.FETCHED : SNAPSHOT_FROM.EARLIER_PAIRING);
          }
          return late(id, ev, r);
        }
        clearTimer(TIMER.STEP);
        dispatch(ev);
      }
    }

    function takeWakeLock() {
      if (!env.wakeLock) return dispatch({ type: EV.FAIL, reason: REASON.NO_WAKE });
      var id = fetches.begin(FETCH.WAKE, SPEC[FETCH.WAKE]);
      setTimer(TIMER.STEP, LIMITS.STEP_MS);
      Promise.resolve().then(function () { return env.wakeLock.request("screen"); }).then(function (sentinel) {
        clock.heard(false, function () { woke(id, sentinel); });
      }, function () {
        clock.heard(true, function () {
          if (!settle(FETCH.WAKE, id)) return;
          clearTimer(TIMER.STEP);
          dispatch({ type: EV.FAIL, reason: REASON.NO_WAKE });
        });
      });
    }

    function woke(id, sentinel) {
      if (!settle(FETCH.WAKE, id)) {
        releaseSentinel(sentinel);
        return;
      }
      clearTimer(TIMER.STEP);
      var wid = nextId();
      wake = { id: wid, sentinel: sentinel };
      sentinel.addEventListener("release", function () {
        if (!wake || wake.id !== wid) return;
        wake = null;
        dispatch({ type: EV.END, reason: REASON.WAKE });
      });
      dispatch({ type: EV.WAKE_OK });
    }

    function releaseSentinel(sentinel) {
      try {
        var p = sentinel.release();
        if (p && p.catch) p.catch(function () { /* already released */ });
      } catch (e) { /* already released */ }
    }

    function releaseWake() {
      if (!wake) return;
      var s = wake.sentinel;
      wake = null;
      releaseSentinel(s);
    }

    function requestGrant() {
      var startedAt = env.now();
      var d = c.dest;
      var id = step(FETCH.GRANT, function (o) {
        return api("POST", PATH.GRANT, { session_id: d.session_id, generation: d.generation, epoch: d.epoch }, o);
      }, { fails: function (r) { return !A.grantOf(r) || !fetches.isCurrent(FETCH.GRANT, id); },
        answer: granted, none: grantUnanswered });
      function granted(r) {
        var got = A.grantOf(r);
        var refused = { type: EV.FAIL, reason: REASON.REFUSED, detail: detail(r) };
        // An abandoned request's 200 may carry a grant, which replaced
        // whatever grant this page holds now: it silences the page.
        if (!settle(FETCH.GRANT, id)) {
          if (r.status === 200) silence(REASON.GRANT_LOST);
          late(id, refused, r);
          return straggled(id, got);
        }
        clearTimer(TIMER.STEP);
        if (!got) return dispatch(refused);
        grant = got;
        setTimer(TIMER.BEAT, LIMITS.BEAT_MS);
        dispatch({ type: EV.GRANT_OK, startedAt: startedAt,
          ctx: { selectedId: deps.selectedId(), visible: deps.visible() } });
      }
      function grantUnanswered() {
        // No response is no proof the hub never read the request: the
        // acquisition is unknown, like one the page gave up on, so it
        // stays a straggler whether this or the step deadline came first.
        if (!fetches.isCurrent(FETCH.GRANT, id)) return;
        clearTimer(TIMER.STEP);
        abandonGrant();
        dispatch({ type: EV.FAIL, reason: REASON.HUB_UNREACHABLE });
      }
    }

    // The grant request is left open when the conversation gives up on it,
    // and never aborted: once the hub has read a request it runs it to
    // completion, behind the state lock if need be, and grants whatever free
    // target it names. Neither an abort nor a page-side timer proves it won't
    // still do so, so Start waits until one of two things does: its response
    // arrives (and a grant in it is released), or a poll shows a new hub
    // epoch (the request named the old one, and its connection died with
    // that hub process). A request that fails with no response, before or
    // after the page gave up on it, leaves Start refused until the epoch
    // changes or the page is reloaded.
    function abandonGrant() {
      var f = fetches.get(FETCH.GRANT);
      if (!f) return false;
      abandon(FETCH.GRANT, false);
      straggler = { id: f.id, epoch: c.dest.epoch };
      return true;
    }

    // An abandoned grant request answered: a grant it brought is released
    // at once, and Start waits for that release to settle.
    function straggled(id, got) {
      if (!got) return forgetStraggler(id);
      release(got).then(function () { forgetStraggler(id); });
    }

    function forgetStraggler(id) {
      if (!straggler || straggler.id !== id) return;
      straggler = null;
      notify();
    }

    // One beat in flight, every BEAT_MS, abandoned after BEAT_ABORT_MS; only
    // the current beat's success renews freshness. Any other answer, a
    // refusal, a 5xx or a 200 that doesn't name the grant beaten, means the
    // grant is gone; no answer at all is left to the freshness deadline.
    function beat() {
      setTimer(TIMER.BEAT, LIMITS.BEAT_MS);
      if (fetches.get(FETCH.BEAT) || !grant) return;
      var startedAt = env.now();
      var named = grant;
      var spec = Object.assign({ bound: setTimer(TIMER.BEAT_ABORT, LIMITS.BEAT_ABORT_MS) }, SPEC[FETCH.BEAT]);
      var id = fetches.run(FETCH.BEAT, spec, function (o) { return api("POST", PATH.BEAT, { grant: named }, o); }, {
        fails: function (r) { return !A.renewed(r, named); }, answer: beaten,
        none: function () { if (settle(FETCH.BEAT, id)) clearTimer(TIMER.BEAT_ABORT); } });
      function beaten(r) {
        var ok = A.renewed(r, named);
        // A beat given up on can still answer: it renews nothing, and a 200
        // that renews nothing silences the page (one whose body was lost
        // already has, at bodyLost).
        if (!settle(FETCH.BEAT, id)) {
          if (r.status === 200 && !ok) silence(REASON.GRANT_LOST);
          return late(id, { type: EV.END, reason: REASON.GRANT_LOST }, r);
        }
        clearTimer(TIMER.BEAT_ABORT);
        if (ok) beatOk(startedAt);
        else silence(REASON.GRANT_LOST);
      }
    }

    function beatOk(startedAt) {
      if (!closing) return dispatch({ type: EV.BEAT_OK, startedAt: startedAt });
      closing.freshUntil = L.renew(closing.freshUntil, startedAt, env.now());
      if (!closing.freshUntil) stopClosing();
    }

    // Settles either way: a stale grant answers 409, and an unreleased one expires on its own.
    function release(id) {
      return api("POST", PATH.RELEASE, { grant: id }, {}).then(function () {}, function () {});
    }

    function requestTicket() {
      var d = c.dest;
      var id = step(FETCH.TICKET, function (o) {
        return api("POST", PATH.TICKET, { session_id: d.session_id, generation: d.generation, epoch: d.epoch }, o);
      }, { fails: function (r) { return !A.ticketOf(r); }, answer: ticketed, none: function () {
        if (!settle(FETCH.TICKET, id)) return;
        clearTimer(TIMER.STEP);
        dispatch({ type: EV.TICKET_FAIL, reason: UNREACHABLE });
      } });
      function ticketed(r) {
        var ticket = A.ticketOf(r);
        var ev = ticket ? { type: EV.TICKET_OK, ticket: ticket } : { type: EV.TICKET_FAIL, reason: detail(r) };
        if (!settle(FETCH.TICKET, id)) {
          if (r.status === 200 && !ticket) silence(REASON.UNUSABLE);
          return late(id, ev, r);
        }
        clearTimer(TIMER.STEP);
        dispatch(ev);
      }
    }

    // Unknown is never retried; a refusal before handoff is final. The
    // answer is dispatched even after an end, so the end reason keeps what
    // it establishes (late). The POST keeps its own deadline, apart from
    // the conversation's timers, whatever becomes of the conversation: at it
    // the POST is aborted, and delivery stays as the end left it.
    function postSend(ticket, text) {
      var id = null;
      function unanswered() {
        if (settle(FETCH.SEND, id) || fetches.takeLate(id).send) dispatch({ type: EV.SEND_UNKNOWN });
      }
      var body = { ticket: ticket, text: text, confirm: L.CONFIRM.VOICE, grant: grant };
      id = fetches.run(FETCH.SEND, Object.assign({ onDeadline: unanswered }, SPEC[FETCH.SEND]), function (o) {
        return api("POST", PATH.SEND, body, o);
      }, { fails: function (r) { return sendEvent(r).type !== EV.SEND_OK; }, answer: function (r) {
        var ev = sendEvent(r);
        if (settle(FETCH.SEND, id)) dispatch(ev);
        else late(id, ev, r);
        if (r.status === 200 && ev.type !== EV.SEND_OK) silence(REASON.UNUSABLE);
      }, none: unanswered });
    }

    // ---- speech -----------------------------------------------------------
    //
    // `utt` is the conversation utterance's role in the state machine, and
    // ends at its end, error, timeout or cut; the acoustic bookkeeping of
    // every utterance this page submits is the sound tracker's (sound_tracker.js).

    // A cancel this page issues: sound is marked uncertain first, then the
    // conversation utterance's id is cleared, then the voice is cancelled.
    // Returns the conversation utterance that was cut, if any.
    function cancelSpeech() {
      var cut = utt;
      if (!cut && !sound.sounding()) return null;
      sound.cancel(function () {
        utt = null;
        clearTimer(TIMER.SPEECH);
      });
      return cut;
    }

    // Stops the conversation's current utterance before its own end.
    function cutSpeech() {
      if (!utt) return false;
      cancelSpeech();
      return true;
    }

    function speak(text, kind) {
      cutSpeech();
      var u = new env.Utterance(text);
      u.lang = "en-US";
      var id = nextId();
      var estimate = L.estimateMs(text);
      var heard = sound.submit(estimate);
      utt = { id: id, u: u, kind: kind, estimate: estimate };
      function mine() { return !!utt && utt.id === id; }
      u.onend = function () {
        heard(false);
        if (!mine()) return notify();
        utt = null;
        clearTimer(TIMER.SPEECH);
        said(kind, true);
      };
      // An error is a stop before the end: uncertain, and cancel as cleanup.
      u.onerror = function () {
        heard(true);
        if (!mine()) return notify();
        utt = null;
        clearTimer(TIMER.SPEECH);
        sound.cancel();
        said(kind, false);
      };
      setTimer(TIMER.SPEECH, estimate * L.SPEECH_DEADLINE_FACTOR);
      try {
        env.synth.speak(u);
      } catch (e) {
        if (mine()) u.onerror({ error: "speak failed" });
      }
    }

    // A late end or error of an utterance already written off: it may have
    // sounded until now, so what a mic heard since is suspect. An open mic,
    // or a draft holding words recognized after the write-off, ends the
    // conversation silently, the draft discarded and never sent; a running
    // one-shot dictation stops and drops its words (deps.lateSound). Either
    // way sound is uncertain again. Otherwise the guard, which the event
    // has moved, is enough.
    function lateSound(entry) {
      var heard = active() && (mic.open() || (!!c.draft && mic.lastFinalAt() >= entry.offAt));
      var dictated = !!(deps.lateSound && deps.lateSound());
      if (!heard && !dictated) return;
      sound.markUncertain(entry.estimate);
      if (heard) dispatch({ type: EV.END, reason: REASON.ECHO });
    }

    // No `end` within twice the estimate: cancel as cleanup and fail.
    function speechTimedOut() {
      var cut = cancelSpeech();
      if (cut) said(cut.kind, false);
    }

    function said(kind, ok) {
      if (kind === SAY.END) return closingDone();
      dispatch(ok ? { type: EV.SAY_END, kind: kind } : { type: EV.SAY_FAILED });
    }

    // ---- the end ----------------------------------------------------------

    // What keeps the grant beaten while the closing cue plays.
    var CLOSING_TIMERS = {};
    CLOSING_TIMERS[TIMER.BEAT] = true;
    CLOSING_TIMERS[TIMER.BEAT_ABORT] = true;

    // `spoken`: the reducer's word that the page decided this end itself
    // (SPOKEN_ENDS); every other end is silent.
    function teardown(reason, spoken) {
      // Every cut ends the conversation, so any cut since the start was on the way here.
      cutSpeech();
      var cut = sound.cuts() !== cutsAtStart;
      mic.stop();
      var held = grant;
      // Even an end the page decided is silent when the grant isn't held
      // and fresh, a sound was cut off, or the page can't be heard or has no
      // voice: a successor may be listening.
      var cue = !!spoken && !!held && !cut && L.fresh(c, env.now()) && deps.visible() && deps.voiceAvailable();
      fetches.lateSend(L.awaitsSend(c) && fetches.get(FETCH.SEND) ? fetches.get(FETCH.SEND).id : null);
      Object.keys(timers).forEach(function (name) { if (!(cue && CLOSING_TIMERS[name])) clearTimer(name); });
      // The grant request stays open so a late grant is released; a late
      // ticket answer is dropped; the send POST runs on to its own deadline,
      // its answer only settling the reason; the beat goes on under the cue.
      fetches.kinds().forEach(function (kind) {
        if (kind === FETCH.GRANT) abandonGrant();
        else if (!(cue && kind === FETCH.BEAT)) abandon(kind, kind !== FETCH.SEND);
      });
      releaseWake();
      interim = "";
      if (!held) return;
      if (!cue) {
        grant = null;
        return release(held);
      }
      // The cue must not outlive the grant: freshness is still checked every
      // tick, and a lapse, any status but 200 (hubStatus) or any other
      // answer that silences the page (silence) cuts the cue short.
      closing = { freshUntil: c.freshUntil };
      setTimer(TIMER.TICK, LIMITS.TICK_MS);
      speak(CUE.ENDED + ": " + reason + ".", SAY.END);
    }

    // The closing cue ended or failed, or was cut: the grant is let go.
    function closingDone() {
      if (!closing) return;
      closing = null;
      clearTimer(TIMER.BEAT);
      clearTimer(TIMER.BEAT_ABORT);
      clearTimer(TIMER.TICK);
      abandon(FETCH.BEAT, true);
      var held = grant;
      grant = null;
      if (held) release(held);
      notify();
    }

    // The closing cue cut short by End, hiding, leaving, unpairing, the
    // grant's lapse, any status but 200 or any other answer that silences the page.
    function stopClosing() {
      if (!closing) return;
      cutSpeech();
      closingDone();
    }

    // ---- public -----------------------------------------------------------

    function active() { return L.active(c); }

    function canStart() {
      if (active() || straggler || utt || sound.busy(env.now())) return false;
      // Not proof of quiet when false, but proof of sound when true.
      return !(env.synth && (env.synth.speaking || env.synth.pending));
    }

    function start(dest) {
      if (!dest || !canStart()) return false;
      cutsAtStart = sound.cuts();
      fetches.forgetLate();
      dispatch({ type: EV.START, dest: Object.freeze({
        host: dest.host, session_id: dest.session_id, generation: dest.generation,
        label: dest.label, epoch: dest.epoch
      }) });
      return true;
    }

    function endIfActive(reason) {
      if (active()) dispatch({ type: EV.END, reason: reason });
      else stopClosing();
    }

    function targetSelected(id) {
      if (active() && id !== c.dest.session_id) dispatch({ type: EV.END, reason: REASON.RETARGET });
    }

    // A page utterance that hasn't ended within twice its estimate is
    // cancelled as cleanup and counts as cut off; its real end or error,
    // whenever it comes, still moves the guard.
    function track(u) {
      sound.track(u, function () {
        var cut = cancelSpeech();
        if (cut) said(cut.kind, false);
        notify();
      });
      // Start's enabled state changed.
      notify();
    }

    function holds() { return active() || !!utt; }

    // The page stopping its own speech (Play, the legacy reply speech)
    // before it speaks again or when it is hidden: through the tracker, so
    // the stopped utterances are marked uncertain and their watchdogs
    // disarmed at once. A conversation's own speech is the controller's.
    function stopPageSpeech() {
      if (!holds()) sound.cancel();
    }

    // For the page's own one-shot Dictate: no utterance of this page queued
    // or playing, the guard after the last real end, error or cancel passed,
    // and no uncertain-sound period running.
    function soundClear() {
      var now = env.now();
      if (utt || sound.busy(now) || now - sound.quietAt() < LIMITS.GUARD_MS) return false;
      return !(env.synth && (env.synth.speaking || env.synth.pending));
    }

    // Why canStart() is false, for the view: a BLOCKER, or null when it is true.
    function blocker() {
      if (active()) return L.BLOCKER.IN_PROGRESS;
      if (straggler) return L.BLOCKER.LETTING_GO;
      return canStart() ? null : L.BLOCKER.NOT_QUIET;
    }

    function view() {
      return {
        phase: c.phase, active: active(), serial: c.serial, dest: c.dest, label: c.label, draft: c.draft,
        interim: interim,
        reason: c.reason, speaking: !!utt, canStart: canStart(), soundClear: soundClear(),
        reply: L.currentReply(c)
      };
    }

    return {
      start: start, canStart: canStart, blocker: blocker, snapshot: snapshot, track: track, view: view, holds: holds,
      soundClear: soundClear, stopPageSpeech: stopPageSpeech, hubStatus: hubStatus, bodyLost: bodyLost,
      endTap: function () { endIfActive(REASON.END_TAPPED); },
      hidden: function () { endIfActive(REASON.HIDDEN); },
      unpaired: function () {
        endIfActive(REASON.UNPAIRED);
        pairing++;
        c = L.forget(c);
        interim = "";
        notify();
      },
      pageExit: function () { endIfActive(REASON.EXITED); },
      targetSelected: targetSelected
    };
  }

  // Timer functions are wrapped: called as env.setTimeout, the browser's own
  // would throw "Illegal invocation".
  function browserEnv(win) {
    return {
      Recognition: win.SpeechRecognition || win.webkitSpeechRecognition, synth: win.speechSynthesis,
      Utterance: win.SpeechSynthesisUtterance, AbortController: win.AbortController,
      now: function () { return win.performance.now(); },
      setTimeout: function (fn, ms) { return win.setTimeout(fn, ms); },
      clearTimeout: function (handle) { win.clearTimeout(handle); },
      wakeLock: (win.navigator && win.navigator.wakeLock) || null
    };
  }

  var exported = { create: create, browserEnv: browserEnv, PATH: PATH, SNAPSHOT_FROM: SNAPSHOT_FROM };
  root.DeskConversation = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
