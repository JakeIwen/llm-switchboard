// The page's own sound, as far as the page can know it (docs/voice-desk.md,
// "Speech output", the completion rule). One tracker per page, owned by the
// conversation controller (conversation.js), which shares it with the tap
// flow's speech through conv.track(). No DOM; node drives it through the
// controller's tests.
//
// Two kinds of bookkeeping meet here. `sounding` is acoustic: every
// utterance this page submitted, the conversation's and the tracked ones,
// until a terminal event (its own `end` or `error`) or a cancel this page
// issued. Listeners stay attached after either, so a real `end` or `error`
// that comes later still moves `quietAt`, which the guard before every mic
// opening measures from: sound may have played until then. An utterance
// stopped before its own end makes sound uncertain for twice its estimate.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");

  // opts: {env: {synth, setTimeout, clearTimeout, now}, onChange(): the
  // quiet rules have passed or a tracked utterance changed state}.
  function create(opts) {
    var env = opts.env;
    var sounding = [];       // submitted utterances with no terminal event yet
    // The latest submission: the one the engine plays, since every page
    // path cancels through cancel() before it speaks.
    var latest = null;
    var quietAt = -Infinity; // the latest end, error or cancel of this page's speech
    var uncertainUntil = 0;  // Start stays refused until then
    var cuts = 0;            // utterances stopped before their end, ever
    var quietHandle = null;  // tells the page when the quiet rules have passed

    function changed() { if (opts.onChange) opts.onChange(); }

    function markUncertain(estimate) {
      cuts++;
      var until = env.now() + estimate * L.SPEECH_DEADLINE_FACTOR;
      if (until > uncertainUntil) uncertainUntil = until;
    }

    // Start's quiet period and the guard pass with time, not with an event:
    // one timer tells the page when they have.
    function soundChanged() {
      if (quietHandle !== null) env.clearTimeout(quietHandle);
      quietHandle = null;
      var wait = Math.max(uncertainUntil, quietAt + L.LIMITS.GUARD_MS) - env.now();
      if (wait > 0) quietHandle = env.setTimeout(function () { quietHandle = null; changed(); }, wait);
    }

    function disarm(entry) {
      if (entry.watchdog === null) return;
      env.clearTimeout(entry.watchdog);
      entry.watchdog = null;
    }

    // A newer submission supersedes the latest one: its watchdog is
    // disarmed at once, so it can never cancel the newer speech.
    function enter(estimate) {
      if (latest) disarm(latest);
      latest = { estimate: estimate, watchdog: null };
      sounding.push(latest);
      return latest;
    }

    // Registers a submitted utterance; the returned `heard(error)` is its
    // own terminal event, early or late.
    function submit(estimate) { return heardBy(enter(estimate)); }

    function heardBy(entry) {
      var estimate = entry.estimate;
      return function heard(error) {
        quietAt = env.now();
        var i = sounding.indexOf(entry);
        if (i >= 0) {
          sounding.splice(i, 1);
          if (error) markUncertain(estimate);
        }
        soundChanged();
      };
    }

    // Every cancel the page issues comes through here, so no utterance is
    // stopped without its bookkeeping. It ends every submitted utterance
    // still sounding: sound is marked uncertain first and their watchdogs
    // disarmed, then `between()` runs (the controller clears its
    // utterance's id there), then the voice is cancelled. With nothing of
    // the page's sounding, the cancel is cleanup and stops nothing it knows of.
    function cancel(between) {
      var stopped = sounding;
      sounding = [];
      stopped.forEach(function (entry) {
        markUncertain(entry.estimate);
        disarm(entry);
      });
      if (between) between();
      if (stopped.length) quietAt = env.now();
      try { env.synth.cancel(); } catch (e) { /* nothing queued */ }
      soundChanged();
    }

    // A page utterance that hasn't ended within twice its estimate is
    // overdue: `overdue()` cancels it as cleanup. The watchdog acts only
    // while that very utterance is the one the engine plays; a superseded
    // or cancelled one has no authority over newer speech. Its real end or
    // error, whenever it comes, still moves the guard.
    function track(u, overdue) {
      var entry = enter(L.estimateMs(u.text || ""));
      var heard = heardBy(entry);
      entry.watchdog = env.setTimeout(function () {
        entry.watchdog = null;
        if (entry === latest && sounding.indexOf(entry) !== -1) overdue();
      }, entry.estimate * L.SPEECH_DEADLINE_FACTOR);
      function done(error) {
        disarm(entry);
        heard(error);
        changed();
      }
      u.addEventListener("end", function () { done(false); });
      u.addEventListener("error", function () { done(true); });
    }

    // Something of this page's may still be sounding at `now`: an utterance
    // with no terminal event, or an uncertain period running.
    function busy(now) { return sounding.length > 0 || now < uncertainUntil; }

    return {
      submit: submit, cancel: cancel, track: track, busy: busy,
      sounding: function () { return sounding.length; },
      quietAt: function () { return quietAt; },
      cuts: function () { return cuts; }
    };
  }

  var exported = { create: create };
  root.DeskSoundTracker = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
