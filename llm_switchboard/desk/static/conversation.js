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
//   endTap()          The full-screen End conversation control; also stops
//                     the closing cue.
//   hidden()          The page was hidden (visibilitychange to hidden).
//   pageExit()        The page received `pagehide` or `freeze`, whatever
//                     visibilityState says: ends any conversation or closing
//                     cue silently and still releases the grant.
//   targetSelected(id) The selection changed; another target ends it.
//   unpaired()        The token was dropped (401, revoke, expiry): ends any
//                     conversation as lost, and drops the snapshot, reply,
//                     draft and label it kept; only the end reason stays.
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
//                     lost, speaking, canStart, soundClear, reply}: everything the
//                     view shows; `reply` is {text, spoken} of the current
//                     request once a snapshot holds it, else null.
//   holds()           A conversation is active or its closing cue plays: the
//                     page disables Dictate, Review, Send, Test, Play and its
//                     own reply speech, and leaves speech to the controller.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");
  var D = root.DeskLogic || require("./desk_logic.js");
  var T = root.DeskSoundTracker || require("./sound_tracker.js");
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

  // A send's outcome from desk_logic.classifySend, as a reducer event.
  var SEND_EVENT = { handed_off: EV.SEND_OK, answered: EV.SEND_OK, not_sent: EV.SEND_REFUSED };

  function detail(r) {
    return r && r.data && typeof r.data.error === "string" ? r.data.error : "HTTP " + (r ? r.status : "?");
  }

  // The hub's refusal in an answer that is not a success: its words, or ""
  // when it gave none (a 200 the page can't use among them), which the
  // reducer judges as a refusal it doesn't know.
  function refusalOf(r) {
    return r && r.data && typeof r.data.error === "string" ? r.data.error : "";
  }

  // A send's answer as a reducer event.
  function sendEvent(r) {
    var k = D.classifySend(r);
    var ev = { type: SEND_EVENT[k.outcome] || EV.SEND_UNKNOWN, request_id: k.request_id,
      state: r.data && r.data.state, reason: detail(r) };
    if (ev.type !== EV.SEND_OK) ev.refusal = refusalOf(r);
    return ev;
  }

  function create(opts) {
    var api = opts.api, env = opts.env, deps = opts.deps;
    var c = L.initial();
    var queue = [];
    var draining = false;
    var seq = 0;
    var rec = null;          // {id, r}
    var utt = null;          // {id, u, kind, estimate}
    var timers = {};         // TIMER name -> {id, handle}
    var fetches = {};        // FETCH kind -> {id, ctl}
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
    RUN[FX.END] = function (fx) { teardown(fx.reason, fx.lost, fx.silent); };

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
    ON_TIMER[TIMER.SEND_ABORT] = function () {
      if (abandon(FETCH.SEND, true)) dispatch({ type: EV.SEND_UNKNOWN });
    };
    ON_TIMER[TIMER.SPEECH] = speechTimedOut;

    function onTimer(name) {
      if (ON_TIMER[name]) ON_TIMER[name]();
      else dispatch({ type: EV.TIMEOUT, name: name });
    }

    // ---- fetches ----------------------------------------------------------

    function begin(kind, abortable) {
      var id = nextId();
      fetches[kind] = { id: id, ctl: abortable && env.AbortController ? new env.AbortController() : null };
      return id;
    }

    function signal(kind) {
      var f = fetches[kind];
      return f && f.ctl ? { signal: f.ctl.signal } : {};
    }

    // True once for the current fetch of that kind; its answer is then used.
    function settle(kind, id) {
      if (!fetches[kind] || fetches[kind].id !== id) return false;
      delete fetches[kind];
      return true;
    }

    // True once for the send POST the last end left open; a newer start forgets it.
    function settleLate(id) {
      if (lateSend === null || lateSend !== id) return false;
      lateSend = null;
      return true;
    }

    // Forgets the current fetch of a kind (its answer will be dropped), then aborts it.
    function abandon(kind, abort) {
      var f = fetches[kind];
      if (!f) return false;
      delete fetches[kind];
      if (abort && f.ctl) f.ctl.abort();
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

    // An answer that proves the grant gone: the conversation ends as lost,
    // or the closing cue stops.
    function grantLost(reason) {
      if (closing) return stopClosing();
      dispatch({ type: EV.END, reason: reason, lost: true });
    }

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
        var got = r.status === 200 && r.data && typeof r.data.grant === "string" ? r.data.grant : null;
        if (!settle(FETCH.GRANT, id)) return straggled(id, got);
        clearTimer(TIMER.STEP);
        if (unauthorized(r)) return;
        if (!got) return dispatch({ type: EV.FAIL, reason: L.withDetail(REASON.REFUSED, detail(r)) });
        grant = got;
        setTimer(TIMER.BEAT, LIMITS.BEAT_MS);
        dispatch({ type: EV.GRANT_OK, startedAt: startedAt,
          ctx: { selectedId: deps.selectedId(), visible: deps.visible() } });
      }, function () {
        // No response is no proof the hub never read the request: the
        // acquisition is unknown, like one the page gave up on, so it
        // stays a straggler whether this or the step deadline came first.
        if (!fetches[FETCH.GRANT] || fetches[FETCH.GRANT].id !== id) return;
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
      var f = fetches[FETCH.GRANT];
      if (!f) return false;
      delete fetches[FETCH.GRANT];
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
    // the current beat's success renews freshness. A refusal means the grant
    // is gone; no answer at all is left to the freshness deadline.
    function beat() {
      setTimer(TIMER.BEAT, LIMITS.BEAT_MS);
      if (fetches[FETCH.BEAT] || !grant) return;
      var id = begin(FETCH.BEAT, true);
      var startedAt = env.now();
      setTimer(TIMER.BEAT_ABORT, LIMITS.BEAT_ABORT_MS);
      api("POST", PATH.BEAT, { grant: grant }, signal(FETCH.BEAT)).then(function (r) {
        if (!settle(FETCH.BEAT, id)) return;
        clearTimer(TIMER.BEAT_ABORT);
        if (unauthorized(r)) return;
        if (r.status === 200) beatOk(startedAt);
        else if (r.status >= 400 && r.status < 500) grantLost(REASON.GRANT_LOST);
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
        if (!settle(FETCH.TICKET, id)) return;
        clearTimer(TIMER.STEP);
        if (unauthorized(r)) return;
        if (r.status === 200 && r.data && typeof r.data.ticket === "string") {
          dispatch({ type: EV.TICKET_OK, ticket: r.data.ticket });
        } else {
          dispatch({ type: EV.TICKET_FAIL, reason: detail(r), refusal: refusalOf(r) });
        }
      }, function () {
        if (!settle(FETCH.TICKET, id)) return;
        clearTimer(TIMER.STEP);
        dispatch({ type: EV.TICKET_FAIL, reason: UNREACHABLE });
      });
    }

    // Unknown is never retried; a refusal before handoff is final. The
    // answer is dispatched even after an end, and after a 401 has ended the
    // conversation, so the end reason keeps what it establishes.
    function postSend(ticket, text) {
      var id = begin(FETCH.SEND, true);
      setTimer(TIMER.SEND_ABORT, LIMITS.SEND_DEADLINE_MS);
      var body = { ticket: ticket, text: text, confirm: L.CONFIRM.VOICE, grant: grant };
      api("POST", PATH.SEND, body, signal(FETCH.SEND)).then(function (r) {
        if (!settle(FETCH.SEND, id) && !settleLate(id)) return;
        clearTimer(TIMER.SEND_ABORT);
        unauthorized(r);
        dispatch(sendEvent(r));
      }, function () {
        if (!settle(FETCH.SEND, id) && !settleLate(id)) return;
        clearTimer(TIMER.SEND_ABORT);
        dispatch({ type: EV.SEND_UNKNOWN });
      });
    }

    // ---- recognition ------------------------------------------------------

    function startRecognizer() {
      // Started only from listening, and never inside the guard: the
      // reducer promises both; a broken promise fails closed.
      if (c.phase !== PHASE.LISTENING) return;
      if (env.now() - sound.quietAt() < LIMITS.GUARD_MS) {
        return dispatch({ type: EV.END, reason: L.withDetail(REASON.REC_FAILED, "guard") });
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
      if (heard) dispatch({ type: EV.END, reason: REASON.ECHO, silent: true });
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

    function teardown(reason, lost, silent) {
      // Every cut ends the conversation, so any cut since the start was on the way here.
      cutSpeech();
      var cut = sound.cuts() !== cutsAtStart;
      stopRecognizer();
      var held = grant;
      // A page that lost its grant, is leaving, cut a sound off, can't be
      // heard or has no voice speaks nothing: a successor may be listening.
      var cue = !!held && !lost && !silent && !cut && L.fresh(c, env.now()) && deps.visible() && deps.voiceAvailable();
      lateSend = c.deliveryOpen && fetches[FETCH.SEND] ? fetches[FETCH.SEND].id : null;
      Object.keys(timers).forEach(function (name) { if (!(cue && CLOSING_TIMERS[name])) clearTimer(name); });
      // The grant request stays open so a late grant is released; a late
      // ticket or send answer is dropped; the beat goes on under the cue.
      Object.keys(fetches).forEach(function (kind) {
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
      // tick, and a lapse or a refused beat cuts the cue short.
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

    // The closing cue cut short by End, hiding, leaving, unpairing or the
    // grant's loss.
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

    function endIfActive(reason, lost, silent) {
      if (active()) dispatch({ type: EV.END, reason: reason, lost: !!lost, silent: !!silent });
      else stopClosing();
    }

    function snapshot(state) {
      if (!state) return;
      if (straggler && state.epoch !== straggler.epoch) forgetStraggler(straggler.id);
      if (active()) return dispatch({ type: EV.SNAPSHOT, snapshot: state });
      // A newer poll that shows the page no longer owns the sound slot cuts
      // the closing cue (sound uncertain) and lets the grant go.
      if (closing && L.ownershipLost(c.dest, { snapshot: state }) &&
          (!c.snap || state.epoch !== c.snap.epoch || state.version >= c.snap.version)) {
        stopClosing();
      }
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
        reason: c.reason, lost: c.lost, speaking: !!utt, canStart: canStart(), soundClear: soundClear(),
        reply: L.currentReply(c)
      };
    }

    return {
      start: start, canStart: canStart, blocker: blocker, snapshot: snapshot, track: track, view: view, holds: holds,
      soundClear: soundClear, stopPageSpeech: stopPageSpeech,
      endTap: function () { endIfActive(REASON.END_TAPPED); },
      hidden: function () { endIfActive(REASON.HIDDEN); },
      unpaired: function () {
        endIfActive(REASON.UNPAIRED, true);
        c = L.forget(c);
        interim = "";
        notify();
      },
      pageExit: function () { endIfActive(REASON.EXITED, false, true); },
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
