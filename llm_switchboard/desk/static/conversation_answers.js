// What the hub's answers to a conversation's calls establish, for the
// controller (conversation.js): the reason an answer shows, the refusal its
// words make, and a send's answer as a reducer event, including whether a
// refusal proves the prompt was not sent. Pure, so node can
// load it; the controller's tests in tests/js/ drive it.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");
  var D = root.DeskLogic || require("./desk_logic.js");
  var EV = L.EV;

  // A send's success from desk_logic.classifySend, as a reducer event.
  var SEND_EVENT = { handed_off: EV.SEND_OK, answered: EV.SEND_OK };

  // The send refusals that prove nothing was handed off: refusals.py's
  // NOT_SENT (`Delivery.NOT_SENT`), which tests/test_desk_conversation_js.py
  // holds this list equal to. Whether the page may speak after one is a
  // separate question, conversation_logic.js's OWNERSHIP_KEPT.
  var NOT_SENT = Object.freeze([
    "ambiguous", "bad confirm", "bad json", "bad request", "bad ticket", "body too large", "busy",
    "channel identity unusable; not sent", "content type must be application/json", "control character",
    "conversation grant not current", "conversation grant not for this target", "empty", "invalid character",
    "leading or trailing whitespace", "length required", "lock file full", "method not allowed",
    "not enrolled", "not registered", "request not pending", "sends off", "stale epoch", "stale generation",
    "starts with a command character", "target changed; not sent", "ticket expired", "too long",
    "too long for a voice send", "too many outstanding requests", "truncated body", "unauthorized",
    "unknown ticket", "voice confirm off", "wrong host", "wrong origin"
  ]);

  function detail(r) {
    return r && r.data && typeof r.data.error === "string" ? r.data.error : "HTTP " + (r ? r.status : "?");
  }

  // The hub's refusal in an answer that is not a success: its words, or ""
  // when it gave none (a 200 the page can't use among them), which the
  // reducer judges as a refusal it doesn't know.
  function refusalOf(r) {
    return r && r.data && typeof r.data.error === "string" ? r.data.error : "";
  }

  // A send answer proves nothing was handed off when it is a 401 (the token
  // is checked before anything else) or a refusal on NOT_SENT. Words the
  // page doesn't know leave delivery unknown.
  function notSent(r) {
    return r.status === 401 || (r.status >= 400 && NOT_SENT.indexOf(refusalOf(r)) !== -1);
  }

  // A send's answer as a reducer event.
  function sendEvent(r) {
    var k = D.classifySend(r);
    var type = SEND_EVENT[k.outcome] || (notSent(r) ? EV.SEND_REFUSED : EV.SEND_UNKNOWN);
    var ev = { type: type, request_id: k.request_id, state: r.data && r.data.state, reason: detail(r) };
    if (ev.type !== EV.SEND_OK) ev.refusal = refusalOf(r);
    return ev;
  }

  var api = { NOT_SENT: NOT_SENT, detail: detail, refusalOf: refusalOf, sendEvent: sendEvent };
  root.DeskConversationAnswers = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
