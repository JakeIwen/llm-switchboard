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
//     api(method, path, body, {signal}) -> Promise<{status, data}>, rejecting
//       when no response came (app.js's `api`, which also drops the token on a 401).
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
//   snapshot(state)   Every state the page's poll receives, whether or not
//                     a conversation is active (a closing cue and an
//                     abandoned grant request use it too).
//   pollUnusable()    The page's poll was answered with anything but a
//                     snapshot (a status other than 200, or no body): like
//                     every hub answer that is not its call's success, it
//                     cuts a closing cue.
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
  var PHASE = L.PHASE, FX = L.FX, EV = L.EV, TIMER = L.TIMER, SAY = L.SAY;
  var REASON = L.REASON, CUE = L.CUE, LIMITS = L.LIMITS;

  var PATH = Object.freeze({
    STATE: "/desk/api/state", GRANT: "/desk/api/conversation", BEAT: "/desk/api/conversation/beat",
    RELEASE: "/desk/api/conversation/end", TICKET: "/desk/api/ticket", SEND: "/desk/api/send"
  });

  // The fetches a conversation can have in flight, one of each at most.
  var FETCH = Object.freeze({
    SNAPSHOT: "snapshot", WAKE: "wake", GRANT: "grant", BEAT: "beat", TICKET: "ticket", SEND: "send"
  });

  var UNREACHABLE = "the hub could not be reached";
  var detail = A.detail, sendEvent = A.sendEvent;

  function create(opts) {
    var api = opts.api, env = opts.env, deps = opts.deps;
    var c = L.initial();
    var queue = [];
    var draining = false;
    var seq = 0;
    var rec = null;          // {id, r}
    var utt = null;          // {id, u, kind, estimate}
    var timers = {};         // TIMER name -> {id, handle}
    var fetches = F.create(nextId, env.AbortController); // FETCH kind -> the one in flight
    var begin = fetches.begin, signal = fetches.signal, settle = fetches.settle, abandon = fetches.abandon;
    var wake = null;         // {id, sentinel}
    var grant = null;        // the grant id this conversation holds
    // {freshUntil} while the closing cue plays: the grant is still held, and
    // beaten and checked like a conversation's, until the cue ends or fails.
    var closing = null;
    // {id, epoch}: an abandoned grant request, or the release of the grant
    // it brought. Start waits for it, so it can't reach the hub after a
    // newer acquisition and replace that grant (the hub keeps the latest).
    var straggler = null;
    // The id of the send POST an end left unanswered with delivery open:
    // its answer may still settle what the end reason says, and nothing else.
    var lateSend = null;
    var sound = T.create({ env: env, onChange: notify, onLate: lateSound });
    var cutsAtStart = 0;     // sound.cuts() at the Start tap: more means this conversation cut one off
    var interim = "";
    var lastFinalAt = -Infinity; // when the conversation's mic last gave a final result

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
    RUN[FX.LISTEN] = startRecognizer;
    RUN[FX.STOP_LISTEN] = stopRecognizer;
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

    function setTimer(name, ms) {
      clearTimer(name);
      var id = nextId();
      var handle = env.setTimeout(function () {
        if (!timers[name] || timers[name].id !== id) return;
        delete timers[name];
        onTimer(name);
      }, ms);
      timers[name] = { id: id, handle: handle };
    }

    function clearTimer(name) {
      var t = timers[name];
      if (!t) return;
      delete timers[name];
      env.clearTimeout(t.handle);
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

    // True once for the send POST the last end left open; a newer start forgets it.
    function settleLate(id) {
      if (lateSend === null || lateSend !== id) return false;
      lateSend = null;
      return true;
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

    // An answer that proves the grant gone: the conversation ends, or the
    // closing cue stops.
    function grantLost(reason) {
      if (closing) return stopClosing();
      dispatch({ type: EV.END, reason: reason });
    }

    // Every answer the hub gives on a conversation's paths, the current
    // conversation's or one it abandoned: one that is not its call's
    // validated success cuts a closing cue at once (sound uncertain) and
    // lets the grant go, and nothing replaces it. Nothing the hub says
    // permits speech; while a conversation runs, its own reducer ends it
    // silently on the same answers.
    function answered(ok) { if (!ok) stopClosing(); }

    // A 401 means the token is gone, whatever the call: the grant went with it.
    function unauthorized(r) {
      if (!r || r.status !== 401) return false;
      grantLost(REASON.UNPAIRED);
      return true;
    }

    function fetchStartSnapshot() {
      var id = begin(FETCH.SNAPSHOT, true);
      setTimer(TIMER.STEP, LIMITS.STEP_MS);
      api("GET", PATH.STATE, undefined, signal(FETCH.SNAPSHOT)).then(function (r) {
        if (!settle(FETCH.SNAPSHOT, id)) return;
        clearTimer(TIMER.STEP);
        if (unauthorized(r)) return;
        if (r.status === 200 && r.data) dispatch({ type: EV.SNAPSHOT_FETCHED, snapshot: r.data });
        else dispatch({ type: EV.FAIL, reason: REASON.HUB_UNREACHABLE });
      }, function () {
        if (!settle(FETCH.SNAPSHOT, id)) return;
        clearTimer(TIMER.STEP);
        dispatch({ type: EV.FAIL, reason: REASON.HUB_UNREACHABLE });
      });
    }

    function takeWakeLock() {
      if (!env.wakeLock) return dispatch({ type: EV.FAIL, reason: REASON.NO_WAKE });
      var id = begin(FETCH.WAKE, false);
      setTimer(TIMER.STEP, LIMITS.STEP_MS);
      Promise.resolve().then(function () { return env.wakeLock.request("screen"); }).then(function (sentinel) {
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
      }, function () {
        if (!settle(FETCH.WAKE, id)) return;
        clearTimer(TIMER.STEP);
        dispatch({ type: EV.FAIL, reason: REASON.NO_WAKE });
      });
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
      var id = begin(FETCH.GRANT, false);
      var startedAt = env.now();
      var d = c.dest;
      setTimer(TIMER.STEP, LIMITS.STEP_MS);
      api("POST", PATH.GRANT, { session_id: d.session_id, generation: d.generation, epoch: d.epoch }, {}).then(function (r) {
        var got = A.grantOf(r);
        // An abandoned request's grant, if any, replaced whatever grant this
        // page holds now: no success of the conversation it would cut.
        if (!settle(FETCH.GRANT, id)) {
          answered(false);
          return straggled(id, got);
        }
        clearTimer(TIMER.STEP);
        if (unauthorized(r)) return;
        if (!got) return dispatch({ type: EV.FAIL, reason: REASON.REFUSED, detail: detail(r) });
        grant = got;
        setTimer(TIMER.BEAT, LIMITS.BEAT_MS);
        dispatch({ type: EV.GRANT_OK, startedAt: startedAt,
          ctx: { selectedId: deps.selectedId(), visible: deps.visible() } });
      }, function () {
        // No response is no proof the hub never read the request: the
        // acquisition is unknown, like one the page gave up on, so it
        // stays a straggler whether this or the step deadline came first.
        if (!fetches.isCurrent(FETCH.GRANT, id)) return;
        clearTimer(TIMER.STEP);
        abandonGrant();
        dispatch({ type: EV.FAIL, reason: REASON.HUB_UNREACHABLE });
      });
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
      var id = begin(FETCH.BEAT, true);
      var startedAt = env.now();
      var named = grant;
      setTimer(TIMER.BEAT_ABORT, LIMITS.BEAT_ABORT_MS);
      api("POST", PATH.BEAT, { grant: named }, signal(FETCH.BEAT)).then(function (r) {
        if (!settle(FETCH.BEAT, id)) return;
        clearTimer(TIMER.BEAT_ABORT);
        if (unauthorized(r)) return;
        if (A.renewed(r, named)) beatOk(startedAt);
        else grantLost(REASON.GRANT_LOST);
      }, function () {
        if (settle(FETCH.BEAT, id)) clearTimer(TIMER.BEAT_ABORT);
      });
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
      var id = begin(FETCH.TICKET, true);
      var d = c.dest;
      setTimer(TIMER.STEP, LIMITS.STEP_MS);
      api("POST", PATH.TICKET, { session_id: d.session_id, generation: d.generation, epoch: d.epoch },
        signal(FETCH.TICKET)).then(function (r) {
        var ticket = A.ticketOf(r);
        if (!settle(FETCH.TICKET, id)) return answered(!!ticket);
        clearTimer(TIMER.STEP);
        if (unauthorized(r)) return;
        if (ticket) {
          dispatch({ type: EV.TICKET_OK, ticket: ticket });
        } else {
          dispatch({ type: EV.TICKET_FAIL, reason: detail(r) });
        }
      }, function () {
        if (!settle(FETCH.TICKET, id)) return;
        clearTimer(TIMER.STEP);
        dispatch({ type: EV.TICKET_FAIL, reason: UNREACHABLE });
      });
    }

    // Unknown is never retried; a refusal before handoff is final. The
    // answer is dispatched even after an end, and after a 401 has ended the
    // conversation, so the end reason keeps what it establishes. The POST
    // keeps its own deadline, apart from the conversation's timers, whatever
    // becomes of the conversation: at it the POST is aborted, and delivery
    // stays unknown for whichever conversation still listens for it.
    function postSend(ticket, text) {
      var id = begin(FETCH.SEND, true);
      var ctl = fetches.get(FETCH.SEND).ctl;
      var deadline = env.setTimeout(function () {
        var mine = settle(FETCH.SEND, id) || settleLate(id);
        if (ctl) ctl.abort();
        if (mine) dispatch({ type: EV.SEND_UNKNOWN });
      }, LIMITS.SEND_DEADLINE_MS);
      var body = { ticket: ticket, text: text, confirm: L.CONFIRM.VOICE, grant: grant };
      api("POST", PATH.SEND, body, signal(FETCH.SEND)).then(function (r) {
        env.clearTimeout(deadline);
        var ev = sendEvent(r);
        var mine = settle(FETCH.SEND, id) || settleLate(id);
        if (mine) {
          unauthorized(r);
          dispatch(ev);
        }
        answered(ev.type === EV.SEND_OK);
      }, function () {
        env.clearTimeout(deadline);
        if (!settle(FETCH.SEND, id) && !settleLate(id)) return;
        dispatch({ type: EV.SEND_UNKNOWN });
      });
    }

    // ---- recognition ------------------------------------------------------

    function startRecognizer() {
      // Started only from listening, and never inside the guard: the
      // reducer promises both; a broken promise fails closed.
      if (c.phase !== PHASE.LISTENING) return;
      if (env.now() - sound.quietAt() < LIMITS.GUARD_MS) {
        return dispatch({ type: EV.END, reason: REASON.REC_FAILED, detail: "guard" });
      }
      stopRecognizer();
      var r;
      try {
        r = new env.Recognition();
      } catch (e) {
        return dispatch({ type: EV.REC_START_FAILED });
      }
      var id = nextId();
      rec = { id: id, r: r };
      function mine() { return !!rec && rec.id === id; }
      r.lang = "en-US";
      r.continuous = false;
      r.interimResults = true;
      r.maxAlternatives = 1;
      r.onaudiostart = function () { if (mine()) dispatch({ type: EV.REC_AUDIOSTART }); };
      r.onresult = function (e) {
        if (!mine()) return;
        for (var i = e.resultIndex; i < e.results.length; i++) {
          var result = e.results[i];
          if (result.isFinal) lastFinalAt = env.now();
          dispatch({ type: EV.RESULT, text: result[0].transcript, final: !!result.isFinal });
        }
      };
      r.onerror = function (e) { if (mine()) dispatch({ type: EV.REC_ERROR, error: e && e.error }); };
      r.onend = function () {
        if (!mine()) return;
        rec = null;
        dispatch({ type: EV.REC_END });
      };
      try {
        r.start();
      } catch (e) {
        if (!mine()) return;
        rec = null;
        dispatch({ type: EV.REC_START_FAILED });
      }
    }

    function stopRecognizer() {
      if (!rec) return;
      var r = rec.r;
      rec = null;
      try { r.abort(); } catch (e) { /* already stopped */ }
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
      var heard = active() && (!!rec || (!!c.draft && lastFinalAt >= entry.offAt));
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
      stopRecognizer();
      var held = grant;
      // Even an end the page decided is silent when the grant isn't held
      // and fresh, a sound was cut off, or the page can't be heard or has no
      // voice: a successor may be listening.
      var cue = !!spoken && !!held && !cut && L.fresh(c, env.now()) && deps.visible() && deps.voiceAvailable();
      lateSend = c.deliveryOpen && fetches.get(FETCH.SEND) ? fetches.get(FETCH.SEND).id : null;
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
      // tick, and a lapse, or any hub answer that is not its call's success,
      // cuts the cue short.
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
    // grant's lapse or any hub answer that is not its call's success.
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
      lateSend = null;
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

    function snapshot(state) {
      if (!state) return;
      if (straggler && state.epoch !== straggler.epoch) forgetStraggler(straggler.id);
      if (active()) return dispatch({ type: EV.SNAPSHOT, snapshot: state });
      // Only a snapshot that shows the target usable leaves a closing cue
      // playing, whichever poll it answers.
      if (closing) answered(!L.targetProblem(c.dest, state));
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
      soundClear: soundClear, stopPageSpeech: stopPageSpeech, pollUnusable: function () { answered(false); },
      endTap: function () { endIfActive(REASON.END_TAPPED); },
      hidden: function () { endIfActive(REASON.HIDDEN); },
      unpaired: function () {
        endIfActive(REASON.UNPAIRED);
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

  var exported = { create: create, browserEnv: browserEnv, PATH: PATH };
  root.DeskConversation = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
