// Pure decisions for the tablet page, kept apart from the DOM so node can
// test them (tests/test_desk_logic_js.py). Same export pattern as text_rule.js.
(function (root) {
  "use strict";

  // Errors the hub raises before it creates a request: the text was never
  // handed to a session, so "not sent" is definitive. Anything else (timeout,
  // 5xx, an unrecognised body) leaves the outcome open.
  var REFUSALS = [
    "sends off", "busy", "bad ticket", "stale epoch", "stale generation", "unknown ticket",
    "ticket expired", "not registered", "ambiguous",
    "target changed; not sent", "too many outstanding requests",
    // text_rule.py reasons
    "empty", "too long", "invalid character", "leading or trailing whitespace",
    "control character", "starts with a command character"
  ];

  var UNKNOWN_TEXT = "Delivery unknown: the hub did not confirm the hand-off. Check the target and its replies. It is not retried.";
  var TICKET_USED_TEXT = "Ticket already used; check the original request.";

  // r is {status, data} from the send call, or null when no response arrived.
  function classifySend(r) {
    if (!r) {
      return { outcome: "unknown", tone: "warn",
        text: "Delivery unknown: no answer from the hub (timeout or dropped connection). Check the target and its replies. It is not retried." };
    }
    var err = r.data && typeof r.data.error === "string" ? r.data.error : null;
    if (r.status === 200) {
      var d = r.data;
      if (d && typeof d.request_id === "string" && d.state === "handed_off") {
        return { outcome: "handed_off", tone: "ok", request_id: d.request_id,
          text: "Handed off; waiting for the reply." };
      }
      if (d && typeof d.request_id === "string" && d.state === "answered") {
        return { outcome: "answered", tone: "ok", request_id: d.request_id, text: "Answered." };
      }
      return { outcome: "unknown", tone: "warn", text: UNKNOWN_TEXT };
    }
    // A consumed ticket says nothing about the first attempt, which may have gone through.
    if (r.status === 409 && err === "ticket already used") {
      return { outcome: "ticket_used", tone: "warn", text: TICKET_USED_TEXT };
    }
    if (r.status === 401) {
      return { outcome: "not_sent", tone: "bad", text: "Not sent: this tablet is not authorised." };
    }
    if (r.status >= 400 && r.status < 500 && err !== null && REFUSALS.indexOf(err) !== -1) {
      return { outcome: "not_sent", tone: "bad", text: "Not sent: " + err + "." };
    }
    return { outcome: "unknown", tone: "warn", text: UNKNOWN_TEXT };
  }

  function describeRequestState(state) {
    var map = {
      pending: "Accepted, not yet delivered",
      handed_off: "Handed off; waiting for the reply",
      answered: "Answered",
      unknown: "Delivery unknown",
      expired: "Not handed off (safe to send again)"
    };
    return map[state] || state;
  }

  // The clear confirmation names one lock. It stays valid only while the
  // target still shows exactly that lock.
  function freezeClear(target) {
    if (!target || !target.lock) return null;
    return { session_id: target.session_id, lock_id: target.lock.lock_id, request_id: target.lock.request_id };
  }

  function clearStillValid(frozen, target) {
    return !!frozen && !!target && !!target.lock &&
      target.session_id === frozen.session_id && target.lock.lock_id === frozen.lock_id;
  }

  // Whether the "handed off, waiting" banner for requestId is over. A request
  // missing from history counts only when the snapshot is known to postdate the
  // send acknowledgment: it is at least as new as the fresh snapshot fetched
  // right after the ack (ackVersion), or its poll started after the ack
  // (pollSeq > ackSeq; both are the page's request counters). A snapshot
  // from before the ack never knew the request, so it never settles the banner.
  function waitingOver(requestId, snapshot, ackVersion, pollSeq, ackSeq) {
    var rq = snapshot.requests.filter(function (x) { return x.request_id === requestId; })[0];
    if (rq) return rq.state !== "handed_off";
    if (typeof ackVersion === "number" && snapshot.version >= ackVersion) return true;
    return typeof pollSeq === "number" && typeof ackSeq === "number" && pollSeq > ackSeq;
  }

  // Keeps only ids still present in the current request history.
  function pruneIds(idSet, requests) {
    var live = {};
    requests.forEach(function (rq) { live[rq.request_id] = true; });
    Array.from(idSet).forEach(function (id) { if (!live[id]) idSet.delete(id); });
  }

  var api = {
    classifySend: classifySend, describeRequestState: describeRequestState,
    freezeClear: freezeClear, waitingOver: waitingOver, clearStillValid: clearStillValid, pruneIds: pruneIds
  };
  root.DeskLogic = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
