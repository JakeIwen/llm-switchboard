// What the hub's answers to a conversation's calls establish, for the
// controller (conversation.js): the reason an answer shows, the refusal its
// words make, and a send's answer as a reducer event. Pure, so node can
// load it; the controller's tests in tests/js/ drive it.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");
  var D = root.DeskLogic || require("./desk_logic.js");
  var EV = L.EV;

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

  var api = { detail: detail, refusalOf: refusalOf, sendEvent: sendEvent };
  root.DeskConversationAnswers = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
