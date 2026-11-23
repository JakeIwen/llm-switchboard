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
//     api(method, path, body, {signal}) -> Promise<{status, data}>, rejecting
//       when no response came (app.js's `api`, which also drops the token on a 401).
//     env: {Recognition, synth, Utterance, setTimeout, clearTimeout, now,
//       wakeLock, AbortController}; `now` is a monotonic ms clock
//       (performance.now), `wakeLock` is navigator.wakeLock or null.
//     deps: {selectedId(), visible(), voiceAvailable(), onChange(view)}:
//       read at the moments the plan checks them; onChange after every change.
//   start(dest)       The Start tap. dest = {host, session_id, generation,
//                     label, epoch} frozen from the tapped control. False
//                     (and nothing happens) unless canStart().
//   canStart()        No conversation, no utterance of this page queued or
//                     playing, and no uncertain sound: the Start control's
//                     enabled state.
//   snapshot(state)   Every state the page's poll receives (ignored when no
//                     conversation is active).
//   endTap()          The full-screen End conversation control; also stops
//                     the closing cue.
//   hidden()          The page was hidden, frozen or is unloading.
//   targetSelected(id) The selection changed; another target ends it.
//   unpaired()        The token was dropped (401, revoke, expiry).
//   track(utterance)  Before the page speaks any utterance of its own (Play,
//                     the legacy reply speech), so its end, error or cancel
//                     counts toward the guard and Start's quiet rule.
//   view()            {phase, active, dest, draft, interim, reason, lost,
//                     speaking, canStart}: everything the view shows. While
//                     `active`, the page disables Dictate, Review, Send, Test,
//                     Play and its own reply speech.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");
  var D = root.DeskLogic || require("./desk_logic.js");
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
    var closingGrant = null; // held until the closing cue ends or fails
    var quietAt = -Infinity; // the latest end, error or cancel of this page's speech
    var uncertainUntil = 0;  // Start stays refused until then
    var cutOff = false;      // this conversation stopped an utterance before its end
    var quietHandle = null;
    var tracked = [];        // the page's own utterances still playing
    var interim = "";

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
          next.quietAt = quietAt;
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
    RUN[FX.END] = function (fx) { teardown(fx.reason, fx.lost); };

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
    ON_TIMER[TIMER.TICK] = function () { dispatch({ type: EV.TICK }); };
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
      if (abandon(FETCH.SNAPSHOT, true) || abandon(FETCH.GRANT, false)) {
        dispatch({ type: EV.FAIL, reason: REASON.HUB_UNREACHABLE });
      } else if (abandon(FETCH.WAKE, false)) {
        dispatch({ type: EV.FAIL, reason: REASON.NO_WAKE });
      } else if (abandon(FETCH.TICKET, true)) {
        dispatch({ type: EV.TICKET_FAIL, reason: UNREACHABLE });
      }
    }

    // A 401 means the token is gone, whatever the call: the grant went with it.
    function unauthorized(r) {
      if (!r || r.status !== 401) return false;
      dispatch({ type: EV.END, reason: REASON.UNPAIRED, lost: true });
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
        if (!settle(FETCH.GRANT, id)) {
          // A grant that arrives after the end is released at once.
          if (got) release(got);
          return;
        }
        clearTimer(TIMER.STEP);
        if (unauthorized(r)) return;
        if (!got) return dispatch({ type: EV.FAIL, reason: L.withDetail(REASON.REFUSED, detail(r)) });
        grant = got;
        setTimer(TIMER.BEAT, LIMITS.BEAT_MS);
        dispatch({ type: EV.GRANT_OK, startedAt: startedAt,
          ctx: { selectedId: deps.selectedId(), visible: deps.visible(), voice: deps.voiceAvailable() } });
      }, function () {
        if (!settle(FETCH.GRANT, id)) return;
        clearTimer(TIMER.STEP);
        dispatch({ type: EV.FAIL, reason: REASON.HUB_UNREACHABLE });
      });
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
        if (r.status === 200) dispatch({ type: EV.BEAT_OK, startedAt: startedAt });
        else if (r.status >= 400 && r.status < 500) dispatch({ type: EV.END, reason: REASON.GRANT_LOST, lost: true });
      }, function () {
        if (settle(FETCH.BEAT, id)) clearTimer(TIMER.BEAT_ABORT);
      });
    }

    function release(id) {
      api("POST", PATH.RELEASE, { grant: id }, {}).then(function () { /* a stale grant answers 409 */ },
        function () { /* it expires on its own */ });
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
          dispatch({ type: EV.TICKET_FAIL, reason: detail(r) });
        }
      }, function () {
        if (!settle(FETCH.TICKET, id)) return;
        clearTimer(TIMER.STEP);
        dispatch({ type: EV.TICKET_FAIL, reason: UNREACHABLE });
      });
    }

    // Unknown is never retried; a refusal before handoff is final.
    function postSend(ticket, text) {
      var id = begin(FETCH.SEND, true);
      setTimer(TIMER.SEND_ABORT, LIMITS.SEND_DEADLINE_MS);
      var body = { ticket: ticket, text: text, confirm: L.CONFIRM.VOICE, grant: grant };
      api("POST", PATH.SEND, body, signal(FETCH.SEND)).then(function (r) {
        if (!settle(FETCH.SEND, id)) return;
        clearTimer(TIMER.SEND_ABORT);
        if (unauthorized(r)) return;
        var k = D.classifySend(r);
        var type = SEND_EVENT[k.outcome] || EV.SEND_UNKNOWN;
        dispatch({ type: type, request_id: k.request_id, state: r.data && r.data.state, reason: detail(r) });
      }, function () {
        if (!settle(FETCH.SEND, id)) return;
        clearTimer(TIMER.SEND_ABORT);
        dispatch({ type: EV.SEND_UNKNOWN });
      });
    }

    // ---- recognition ------------------------------------------------------

    function startRecognizer() {
      // Started only from listening, and never inside the guard: the
      // reducer promises both; a broken promise fails closed.
      if (c.phase !== PHASE.LISTENING) return;
      if (env.now() - quietAt < LIMITS.GUARD_MS) {
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

    function markUncertain(estimate) {
      cutOff = true;
      var until = env.now() + estimate * L.SPEECH_DEADLINE_FACTOR;
      if (until <= uncertainUntil) return;
      uncertainUntil = until;
      if (quietHandle !== null) env.clearTimeout(quietHandle);
      quietHandle = env.setTimeout(function () { quietHandle = null; notify(); }, until - env.now());
    }

    // Stops the current utterance before its own end: sound is uncertain
    // first, then the id is cleared, then the voice is cancelled.
    function cutSpeech() {
      if (!utt) return false;
      markUncertain(utt.estimate);
      utt = null;
      clearTimer(TIMER.SPEECH);
      quietAt = env.now();
      try { env.synth.cancel(); } catch (e) { /* nothing queued */ }
      return true;
    }

    function speak(text, kind) {
      cutSpeech();
      var u = new env.Utterance(text);
      u.lang = "en-US";
      var id = nextId();
      var estimate = L.estimateMs(text);
      utt = { id: id, u: u, kind: kind, estimate: estimate };
      function mine() { return !!utt && utt.id === id; }
      u.onend = function () {
        if (!mine()) return;
        utt = null;
        clearTimer(TIMER.SPEECH);
        quietAt = env.now();
        said(kind, true);
      };
      u.onerror = function () {
        if (!mine()) return;
        markUncertain(estimate);
        utt = null;
        clearTimer(TIMER.SPEECH);
        quietAt = env.now();
        said(kind, false);
      };
      setTimer(TIMER.SPEECH, estimate * L.SPEECH_DEADLINE_FACTOR);
      try {
        env.synth.speak(u);
      } catch (e) {
        if (mine()) u.onerror({ error: "speak failed" });
      }
    }

    // No `end` within twice the estimate: cancel as cleanup and fail.
    function speechTimedOut() {
      if (!utt) return;
      var kind = utt.kind;
      cutSpeech();
      said(kind, false);
    }

    function said(kind, ok) {
      if (kind === SAY.END) return closingDone();
      dispatch(ok ? { type: EV.SAY_END, kind: kind } : { type: EV.SAY_FAILED });
    }

    // ---- the end ----------------------------------------------------------

    function teardown(reason, lost) {
      // Every cut ends the conversation, so any cut since the start was on the way here.
      cutSpeech();
      var cut = cutOff;
      stopRecognizer();
      Object.keys(timers).forEach(clearTimer);
      // The grant request stays open so a late grant is released; a late
      // ticket or send answer is dropped.
      Object.keys(fetches).forEach(function (kind) { abandon(kind, kind !== FETCH.GRANT && kind !== FETCH.SEND); });
      releaseWake();
      interim = "";
      var held = grant;
      grant = null;
      if (!held) return;
      // A page that lost its grant, cut a sound off, can't be heard or has
      // no voice speaks nothing: a successor may be listening.
      if (!lost && !cut && L.fresh(c, env.now()) && deps.visible() && deps.voiceAvailable()) {
        closingGrant = held;
        speak(CUE.ENDED + ": " + reason + ".", SAY.END);
      } else {
        release(held);
      }
    }

    function closingDone() {
      if (closingGrant) release(closingGrant);
      closingGrant = null;
      notify();
    }

    // The closing cue cut short by End, hiding or unpairing.
    function stopClosing() {
      if (!utt || utt.kind !== SAY.END) return;
      cutSpeech();
      closingDone();
    }

    // ---- public -----------------------------------------------------------

    function active() { return L.active(c); }

    function canStart() {
      if (active() || utt || tracked.length) return false;
      if (env.now() < uncertainUntil) return false;
      // Not proof of quiet when false, but proof of sound when true.
      return !(env.synth && (env.synth.speaking || env.synth.pending));
    }

    function start(dest) {
      if (!dest || !canStart()) return false;
      cutOff = false;
      dispatch({ type: EV.START, dest: Object.freeze({
        host: dest.host, session_id: dest.session_id, generation: dest.generation,
        label: dest.label, epoch: dest.epoch
      }) });
      return true;
    }

    function endIfActive(reason, lost) {
      if (active()) dispatch({ type: EV.END, reason: reason, lost: !!lost });
      else stopClosing();
    }

    function snapshot(state) {
      if (active() && state) dispatch({ type: EV.SNAPSHOT, snapshot: state });
    }

    function targetSelected(id) {
      if (active() && id !== c.dest.session_id) dispatch({ type: EV.END, reason: REASON.RETARGET });
    }

    // A page utterance that never reports its end counts as cut off once
    // twice its estimate has passed.
    function track(u) {
      var estimate = L.estimateMs(u.text || "");
      var entry = { u: u };
      function done(cut) {
        var i = tracked.indexOf(entry);
        if (i < 0) return;
        tracked.splice(i, 1);
        env.clearTimeout(entry.handle);
        quietAt = env.now();
        if (cut) markUncertain(estimate);
        notify();
      }
      entry.handle = env.setTimeout(function () { done(true); }, estimate * L.SPEECH_DEADLINE_FACTOR);
      tracked.push(entry);
      u.addEventListener("end", function () { done(false); });
      u.addEventListener("error", function () { done(true); });
    }

    function view() {
      return {
        phase: c.phase, active: active(), dest: c.dest, draft: c.draft, interim: interim,
        reason: c.reason, lost: c.lost, speaking: !!utt, canStart: canStart()
      };
    }

    return {
      start: start, canStart: canStart, snapshot: snapshot, track: track, view: view,
      endTap: function () { endIfActive(REASON.END_TAPPED); },
      hidden: function () { endIfActive(REASON.HIDDEN); },
      unpaired: function () { endIfActive(REASON.UNPAIRED, true); },
      targetSelected: targetSelected
    };
  }

  var exported = { create: create, PATH: PATH };
  root.DeskConversation = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
