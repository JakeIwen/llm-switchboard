// The tap flow's own sound (docs/voice-desk.md, "Conversation mode"): the
// automatic speech of new replies, Play, and one-shot Dictate. A conversation
// owns every mic and sound while it holds the voice (conversation.js), so
// this file defers to the controller it is given: its speech is tracked by
// conv.track(), and it stays silent while conv.holds(). Text only ever goes
// through textContent; app.js keeps the compose flow and the token.
(function (root) {
  "use strict";

  var Logic = root.DeskLogic || require("./desk_logic.js");
  var L = root.DeskConversationLogic || require("./conversation_logic.js");

  // opts: {win: the page's window, $(id) -> element, conv: the controller,
  // note(text): the compose note, for dictation errors, changed(): dictation
  // started or ended, by any route, so the page re-renders the controls that
  // depend on it}.
  function create(opts) {
    var win = opts.win, $ = opts.$, conv = opts.conv;
    var recognition = null;
    var spoken = new Set(); // request ids whose reply was spoken or marked spoken
    var primed = false;     // the first state after pairing must not speak old replies
    var dictatedFrom = "";  // the draft as it was when the running dictation started

    function voiceAvailable() {
      return "speechSynthesis" in win && win.speechSynthesis.getVoices().length > 0;
    }

    function speechInputAvailable() {
      return typeof win.webkitSpeechRecognition === "function" || typeof win.SpeechRecognition === "function";
    }

    // Every cancel goes through the controller's tracker, which marks the
    // cut-off utterance and disarms its watchdog; a conversation's own
    // speech is the controller's to stop.
    function stopSpeaking() {
      if ("speechSynthesis" in win) conv.stopPageSpeech();
    }

    // Tracked, so the page's own speech counts toward Start's quiet rule.
    function speak(text) {
      if (!voiceAvailable() || !text || conv.holds()) return;
      conv.stopPageSpeech();
      var u = new win.SpeechSynthesisUtterance(text);
      u.lang = "en-US";
      conv.track(u);
      win.speechSynthesis.speak(u);
    }

    // Speaks a reply only when it newly arrives for the target on screen;
    // while conv.holds() it is only marked spoken. A reply to a voice send is
    // the conversation's, by the hub's own record of the request, so it is
    // never voiced here: not after its conversation ended, and not when the
    // send's answer never reached the page. Play still speaks it.
    function repliesArrived(next, selectedId) {
      next.requests.forEach(function (rq) {
        if (!rq.reply || spoken.has(rq.request_id)) return;
        spoken.add(rq.request_id);
        if (rq.confirm === L.CONFIRM.VOICE) return;
        if (primed && rq.session_id === selectedId && win.document.visibilityState === "visible") {
          speak(rq.reply.spoken);
        }
      });
      primed = true;
    }

    // A new pairing: nothing already listed is spoken.
    function forgetReplies() {
      primed = false;
      spoken.clear();
    }

    function pruneReplies(requests) { Logic.pruneIds(spoken, requests); }

    function dictating() { return !!recognition; }

    // Why one-shot Dictate may not open the mic now, or null. The mic opens
    // only when no desk sound may still be playing (the completion rule's
    // guard and quiet period, conv.soundClear()) and no conversation holds
    // the voice; the controller's onChange re-renders when that changes.
    function dictateBlocker() {
      if (!conv.soundClear()) return L.BLOCKER.NOT_QUIET;
      if (conv.holds()) return L.BLOCKER.IN_PROGRESS;
      return null;
    }

    // Ends the running dictation without telling the page; callers do.
    function halt() {
      var r = recognition;
      recognition = null;
      r.onresult = r.onend = r.onerror = null;
      try { r.abort(); } catch (e) { /* already stopped */ }
      $("interim").textContent = "";
      $("dictate").textContent = "Dictate";
    }

    function stopDictation() {
      if (!recognition) return;
      halt();
      opts.changed();
    }

    // A late end or error of the page's own speech while dictating (the
    // controller's deps.lateSound): the mic may have heard the desk, so the
    // dictation stops and the words it added go. Restoring the draft as it
    // was at the start removes only dictated words: the page keeps the draft
    // and Test disabled while dictation runs, so nothing else can change it.
    // True when one was running.
    function lateSound() {
      if (!recognition) return false;
      halt();
      $("draft").value = dictatedFrom;
      opts.note("Dictation stopped: " + L.REASON.ECHO + ". Its words were discarded.");
      opts.changed();
      return true;
    }

    // One recognizer, its final words appended to the draft as they come.
    function startDictation() {
      dictatedFrom = $("draft").value;
      var Ctor = win.SpeechRecognition || win.webkitSpeechRecognition;
      var rec = new Ctor();
      rec.lang = "en-US";
      rec.interimResults = true;
      rec.continuous = false;
      rec.onresult = function (e) {
        var finalText = "";
        var interim = "";
        for (var i = e.resultIndex; i < e.results.length; i++) {
          var piece = e.results[i][0].transcript;
          if (e.results[i].isFinal) finalText += piece; else interim += piece;
        }
        if (finalText) {
          var draft = $("draft");
          var sep = draft.value && !/\s$/.test(draft.value) ? " " : "";
          draft.value += sep + finalText.trim();
        }
        $("interim").textContent = interim;
      };
      // Every error ends a one-shot recognizer, and some browsers send no
      // `end` after one: the dictation ends here, so the draft and Test come
      // back, and a later `end` of this recognizer finds nothing to stop.
      rec.onerror = function (e) {
        if (rec !== recognition) return;
        opts.note("Dictation stopped (" + (e && e.error ? e.error : "error") + "). You can type instead.");
        stopDictation();
      };
      rec.onend = function () {
        if (rec !== recognition) return;
        recognition = null;
        $("interim").textContent = "";
        $("dictate").textContent = "Dictate";
        opts.changed();
      };
      recognition = rec;
      try {
        rec.start();
      } catch (e) {
        recognition = null;
        opts.note("Dictation could not start. You can type instead.");
      }
      $("dictate").textContent = "Stop";
      opts.changed();
    }

    return {
      voiceAvailable: voiceAvailable, speechInputAvailable: speechInputAvailable, stopSpeaking: stopSpeaking,
      speak: speak, repliesArrived: repliesArrived, forgetReplies: forgetReplies, pruneReplies: pruneReplies,
      dictating: dictating, dictateBlocker: dictateBlocker, startDictation: startDictation, stopDictation: stopDictation,
      lateSound: lateSound
    };
  }

  var exported = { create: create };
  root.DeskPageAudio = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
