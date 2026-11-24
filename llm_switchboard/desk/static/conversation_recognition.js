// The conversation's speech recognizer, for the controller (conversation.js):
// at most one at a time, each with its own id, and its callbacks honored only
// while it is the current one. Stopping clears the id BEFORE the abort, and
// the handlers stay attached, so the `end` of an aborted recognizer is seen
// and dropped. No DOM of its own; the controller's tests in tests/js/ drive it.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");
  var PHASE = L.PHASE, EV = L.EV, REASON = L.REASON, LIMITS = L.LIMITS;

  // o: {env, nextId(), dispatch(ev), phase(), quietAt()}: the controller's
  // browser objects, its one id counter, its event queue, the reducer's
  // phase and when this page's sound last went quiet.
  function create(o) {
    var env = o.env;
    var rec = null;              // {id, r}
    var lastFinalAt = -Infinity; // when the conversation's mic last gave a final result

    function start() {
      // Started only from listening, and never inside the guard: the
      // reducer promises both; a broken promise fails closed.
      if (o.phase() !== PHASE.LISTENING) return;
      if (env.now() - o.quietAt() < LIMITS.GUARD_MS) {
        return o.dispatch({ type: EV.END, reason: REASON.REC_FAILED, detail: "guard" });
      }
      stop();
      var r;
      try {
        r = new env.Recognition();
      } catch (e) {
        return o.dispatch({ type: EV.REC_START_FAILED });
      }
      var id = o.nextId();
      rec = { id: id, r: r };
      function mine() { return !!rec && rec.id === id; }
      r.lang = "en-US";
      r.continuous = false;
      r.interimResults = true;
      r.maxAlternatives = 1;
      r.onaudiostart = function () { if (mine()) o.dispatch({ type: EV.REC_AUDIOSTART }); };
      r.onresult = function (e) {
        if (!mine()) return;
        for (var i = e.resultIndex; i < e.results.length; i++) {
          var result = e.results[i];
          if (result.isFinal) lastFinalAt = env.now();
          o.dispatch({ type: EV.RESULT, text: result[0].transcript, final: !!result.isFinal });
        }
      };
      r.onerror = function (e) { if (mine()) o.dispatch({ type: EV.REC_ERROR, error: e && e.error }); };
      r.onend = function () {
        if (!mine()) return;
        rec = null;
        o.dispatch({ type: EV.REC_END });
      };
      try {
        r.start();
      } catch (e) {
        if (!mine()) return;
        rec = null;
        o.dispatch({ type: EV.REC_START_FAILED });
      }
    }

    function stop() {
      if (!rec) return;
      var r = rec.r;
      rec = null;
      try { r.abort(); } catch (e) { /* already stopped */ }
    }

    return {
      start: start, stop: stop,
      open: function () { return !!rec; },
      lastFinalAt: function () { return lastFinalAt; }
    };
  }

  var api = { create: create };
  root.DeskConversationRecognition = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
