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
    "control character", "starts with a command character",
    // a send confirmed by voice (grants.py), refused at begin_send or hand_off
    "bad confirm", "voice confirm off", "conversation grant not current",
    "conversation grant not for this target", "too long for a voice send"
  ];

  // The Pi's own sessions' host (model.HOST).
  var PI = "pi";

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
    return { host: target.host, session_id: target.session_id, lock_id: target.lock.lock_id,
      request_id: target.lock.request_id };
  }

  function clearStillValid(frozen, target) {
    return !!frozen && !!target && !!target.lock && sameTarget(frozen, target) &&
      target.lock.lock_id === frozen.lock_id;
  }

  // A target is (host, session_id). The hub lists one target per session id
  // with its host as an attribute, and an id can move between hosts
  // (unenrolled on one, enrolled on another), so the page's selection, its
  // history, its speech and its edits match both fields: it never shows or
  // speaks one host's request under the other's target.
  function keyOf(t) { return t ? { host: t.host, session_id: t.session_id } : null; }

  function sameTarget(a, b) {
    return !!a && !!b && a.host === b.host && a.session_id === b.session_id;
  }

  function findTarget(snapshot, key) {
    if (!snapshot || !key) return null;
    return snapshot.targets.filter(function (t) { return sameTarget(t, key); })[0] || null;
  }

  function requestsFor(requests, key) {
    return requests.filter(function (rq) { return sameTarget(rq, key); });
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

  // The identity a send is frozen to: the target as shown, in the hub's current epoch.
  function destinationOf(t, epoch) {
    return { host: t.host, session_id: t.session_id, generation: t.generation, label: t.label || t.session_id, epoch: epoch };
  }

  // The badge a target shows: its lock first, then an id two hosts own
  // (which routes nowhere), then its registration. A Mac target without one
  // is "Not connected" (docs/voice-desk.md, "Readiness, Test, and the
  // startup warning on the Mac").
  function badgeFor(t) {
    if (t.lock) {
      return t.lock.earlier_run
        ? { text: "Unknown (earlier hub run)", tone: "warn" }
        : { text: "Busy", tone: "warn" };
    }
    if (t.enrolled_on_two_hosts) return { text: "Enrolled on two hosts", tone: "bad" };
    switch (t.registration) {
      case "replied": return { text: "Replied before", tone: "ok" };
      case "registered": return { text: "Registered, no desk reply yet", tone: "warn" };
      case "ambiguous": return { text: "Ambiguous", tone: "bad" };
      default: return t.host === PI ? { text: "Unavailable", tone: "" } : { text: "Not connected", tone: "" };
    }
  }

  function formatTime(since) {
    if (typeof since !== "number") return "unknown time";
    var d = new Date(since < 1e12 ? since * 1000 : since);
    return d.toLocaleString();
  }

  // Whether the tap flow may write to a target.
  function canCompose(t) {
    return !!t && !t.lock && (t.registration === "replied" || t.registration === "registered");
  }

  // Keeps only ids still present in the current request history.
  function pruneIds(idSet, requests) {
    var live = {};
    requests.forEach(function (rq) { live[rq.request_id] = true; });
    Array.from(idSet).forEach(function (id) { if (!live[id]) idSet.delete(id); });
  }

  var api = {
    REFUSALS: REFUSALS, classifySend: classifySend, describeRequestState: describeRequestState,
    freezeClear: freezeClear, waitingOver: waitingOver, clearStillValid: clearStillValid, pruneIds: pruneIds,
    destinationOf: destinationOf, badgeFor: badgeFor, canCompose: canCompose, formatTime: formatTime,
    PI: PI, keyOf: keyOf, sameTarget: sameTarget, findTarget: findTarget, requestsFor: requestsFor
  };
  root.DeskLogic = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
