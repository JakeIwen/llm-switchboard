// Conversation mode's view (docs/voice-desk.md, "Conversation mode"): the
// Start conversation control on the selected session, and the full-screen
// overlay while a conversation exists and after it ends. It renders the
// controller's view() and forwards taps; every decision belongs to the
// controller (conversation.js) or conversation_logic.js. Text only ever goes
// through textContent.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");
  var PHASE = L.PHASE;

  // Each phase in plain words; the mic is open only while listening.
  var STATE_TEXT = {};
  STATE_TEXT[PHASE.STARTING] = "Starting. The mic is closed.";
  STATE_TEXT[PHASE.LISTENING] = "Listening.";
  STATE_TEXT[PHASE.READING] = "Speaking. The mic is closed.";
  STATE_TEXT[PHASE.SENDING] = "Sending. The mic is closed.";
  STATE_TEXT[PHASE.WAITING] = "Sent. Waiting for the reply. The mic is closed.";
  STATE_TEXT[PHASE.SPEAKING] = "Reading the reply. The mic is closed.";

  var END_LABEL = { active: "End conversation", ended: "Close" };

  // doc: the page's document. taps: {start(), end()}, called on the Start and
  // End/Close controls.
  function create(doc, taps) {
    function $(id) { return doc.getElementById(id); }
    var last = null;      // the last render's arguments, for the Close tap
    var closed = null;    // the frozen dest of an ended conversation whose overlay was closed

    $("conv-start").addEventListener("click", function () { taps.start(); });
    $("conv-end").addEventListener("click", function () {
      // Close hides an ended conversation's overlay; a new Start brings a new dest.
      if (last && !last.v.active) closed = last.v.dest;
      taps.end();
      if (last) render(last.v, last.blocker);
    });

    // v: the controller's view(). blocker: startBlocker() for the selected
    // target, or null when Start is offered.
    function render(v, blocker) {
      last = { v: v, blocker: blocker };
      $("conv-start").disabled = !!blocker;
      var why = $("conv-why");
      why.hidden = !blocker;
      why.textContent = blocker ? "Start conversation is unavailable: " + blocker + "." : "";

      var shown = v.phase !== PHASE.OFF && v.dest !== closed;
      $("conv-overlay").hidden = !shown;
      if (!shown) return;
      var ended = v.phase === PHASE.ENDED;
      $("conv-dest").textContent = v.dest.label;
      var state = $("conv-state");
      state.textContent = ended ? L.CUE.ENDED + ": " + v.reason + "." : STATE_TEXT[v.phase];
      state.setAttribute("data-phase", v.phase);
      $("conv-commands").hidden = ended;
      $("conv-draft-area").hidden = ended;
      $("conv-draft").textContent = v.draft;
      $("conv-interim").textContent = v.interim;
      $("conv-end").textContent = ended ? END_LABEL.ended : END_LABEL.active;
    }

    return { render: render };
  }

  var exported = { create: create };
  root.DeskConversationView = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
