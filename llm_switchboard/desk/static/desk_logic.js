// Pure decisions for the tablet page, kept apart from the DOM so node can
// test them (tests/test_desk_logic_js.py). Same export pattern as text_rule.js.
(function (root) {
  "use strict";

  // Errors the hub raises before it creates a request: the text was never
  // handed to a session, so "not sent" is definitive. Anything else (timeout,
  // 5xx, an unrecognised body) leaves the outcome open.
  var REFUSALS = [
    "sends off", "busy", "bad ticket", "stale epoch", "stale generation", "unknown ticket",
    "ticket expired", "ticket already used", "not registered", "ambiguous",
    "target changed; not sent", "too many outstanding requests",
    // text_rule.py reasons
    "empty", "too long", "invalid character", "leading or trailing whitespace",
    "control character", "starts with a command character"
  ];

  var UNKNOWN_TEXT = "Result unknown: the request may have been delivered. Check the target and its replies. It is not retried.";

  // r is {status, data} from the send call, or null when no response arrived.
  function classifySend(r) {
    if (!r) {
      return { outcome: "unknown", tone: "warn",
        text: "Result unknown: no answer from the hub (timeout or dropped connection), so the request may have been delivered. Check the target and its replies. It is not retried." };
    }
    var err = r.data && typeof r.data.error === "string" ? r.data.error : null;
    if (r.status === 200) {
      var d = r.data;
      if (d && typeof d.request_id === "string" && d.state === "handed_off") {
        return { outcome: "handed_off", tone: "ok", request_id: d.request_id,
          text: "Delivered to the session, waiting for its reply." };
      }
      if (d && typeof d.request_id === "string" && d.state === "answered") {
        return { outcome: "answered", tone: "ok", request_id: d.request_id, text: "Answered." };
      }
      return { outcome: "unknown", tone: "warn", text: UNKNOWN_TEXT };
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
      handed_off: "Delivered to the session, waiting for its reply",
      answered: "Answered",
      unknown: "Result unknown: delivered, no reply recorded",
      expired: "Not delivered (safe to send again)"
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

  // Keeps only ids still present in the current request history.
  function pruneIds(idSet, requests) {
    var live = {};
    requests.forEach(function (rq) { live[rq.request_id] = true; });
    Array.from(idSet).forEach(function (id) { if (!live[id]) idSet.delete(id); });
  }

  var api = {
    classifySend: classifySend, describeRequestState: describeRequestState,
    freezeClear: freezeClear, clearStillValid: clearStillValid, pruneIds: pruneIds
  };
  root.DeskLogic = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
