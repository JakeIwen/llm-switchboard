// What the hub's answers to a conversation's calls establish, for the
// controller (conversation.js): the reason an answer shows, whether a
// grant, a beat or a ticket succeeded, and a send's answer as a reducer
// event, including whether a refusal proves the prompt was not sent. None
// of them permits speech: an answer is either its call's validated
// success or something that silences the page. Pure, so node can load it;
// the controller's tests in tests/js/ drive it.
(function (root) {
  "use strict";

  var L = root.DeskConversationLogic || require("./conversation_logic.js");
  var D = root.DeskLogic || require("./desk_logic.js");
  var EV = L.EV;

  // A send's success from desk_logic.classifySend, as a reducer event.
  var SEND_EVENT = { handed_off: EV.SEND_OK, answered: EV.SEND_OK };

  // The send refusals that prove nothing was handed off: refusals.py's
  // NOT_SENT (`Delivery.NOT_SENT`), which tests/test_desk_conversation_js.py
  // holds this list equal to. It decides only what the screen says; the
  // page speaks after none of them.
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
  // when it gave none (a 200 the page can't use among them).
  function refusalOf(r) {
    return r && r.data && typeof r.data.error === "string" ? r.data.error : "";
  }

  // A 200 is an operation's success only in the shape that success has;
  // anything else is an answer that failed, never a success. Acquisition
  // needs a grant the hub could have issued:
  function grantOf(r) {
    var g = r.status === 200 && r.data ? r.data.grant : null;
    return typeof g === "string" && L.GRANT_ID.test(g) ? g : null;
  }

  // a beat renews only the very grant it named;
  function renewed(r, grant) {
    return r.status === 200 && !!r.data && r.data.grant === grant;
  }

  // and a ticket is a 200 naming one.
  function ticketOf(r) {
    var t = r.status === 200 && r.data ? r.data.ticket : null;
    return typeof t === "string" ? t : null;
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
    return { type: type, request_id: k.request_id, state: r.data && r.data.state, reason: detail(r) };
  }

  var api = {
    NOT_SENT: NOT_SENT, detail: detail, grantOf: grantOf, renewed: renewed, ticketOf: ticketOf, sendEvent: sendEvent
  };
  root.DeskConversationAnswers = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
