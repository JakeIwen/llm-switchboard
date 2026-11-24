// The tablet page's words for the Mac link (docs/voice-desk.md, "What the
// desk page shows"): host badges, the link lines, the unattributed refusal
// lines, the Mac hint, the host choice and which enrollment route an edit
// takes. Pure, so node can test it without a DOM; link_view.js and
// enroll_view.js render what it decides. Same export pattern as desk_logic.js.
(function (root) {
  "use strict";

  var Logic = root.DeskLogic || require("./desk_logic.js");
  var PI = Logic.PI;

  // The page's button and its confirmation; the hub's CLI prints the same
  // notice (remote_enrollment.ENROLL_NOW_NOTICE, held equal by a test).
  var ENROLL_NOW = "Enroll now";
  var ENROLL_NOW_NOTICE = "Enroll now: this running Mac session becomes reachable from the desk immediately";
  // A phase 1 hub answers the host-qualified routes 404; the CLI says the same.
  var OLDER_HUB = "The running desk hub is older and can't enroll Mac sessions; nothing changed.";
  var MAC_HINT = "Launch it with claudep --desk on the Mac and press Enter at the warning there.";
  var LINK_UP = "up", LINK_DOWN = "down", VERIFIER_UNAVAILABLE = "verifier_unavailable";
  var VERSION_CODE = "version";

  function hostLabel(host) { return host === PI ? "Pi" : String(host); }

  function isMac(host) { return host !== PI; }

  function links(state) { return state && Array.isArray(state.links) ? state.links : []; }

  function linkFor(state, host) {
    return links(state).filter(function (l) { return l.host === host; })[0] || null;
  }

  // One host's link line: {text, tone}.
  function linkLine(link) {
    if (link.state === LINK_UP) return { text: "Mac link up", tone: "ok" };
    if (link.state === VERIFIER_UNAVAILABLE) {
      return { text: "Mac links unavailable: the Pi can't verify Mac keys (OpenSSH 8.1 or later is needed)",
        tone: "bad" };
    }
    var since = typeof link.since === "number" ? " since " + Logic.formatTime(link.since) : "";
    return { text: "Mac link down" + since, tone: "warn" };
  }

  function speaks(versions) {
    return Array.isArray(versions) && versions.length ? versions.join(" or ") : null;
  }

  // One unattributed refusal, `{code, count, last, versions, hub_versions}`
  // (link_admission.AdmissionRefusals): never under a host, since nothing
  // proved which Mac it came from.
  function refusalLine(entry) {
    var last = ", last at " + Logic.formatTime(entry.last);
    var relay = speaks(entry.versions), hub = speaks(entry.hub_versions);
    if (entry.code === VERSION_CODE && relay && hub) {
      return "A Mac link was refused: version mismatch (the relay speaks " + relay + ", the hub speaks " + hub +
        "; update both machines)" + last;
    }
    var times = entry.count === 1 ? "1 time" : entry.count + " times";
    return "A Mac link was refused: " + entry.code + ", " + times + last;
  }

  function refusalLines(state) {
    var entries = state && Array.isArray(state.admission_refusals) ? state.admission_refusals : [];
    return entries.map(refusalLine);
  }

  // The hosts an enroll can name: `pi` and the hosts in `links`.
  function hostChoices(state) {
    var hosts = [PI];
    links(state).forEach(function (l) { if (hosts.indexOf(l.host) === -1) hosts.push(l.host); });
    return hosts;
  }

  // The hint a Mac target without a registration shows, with its link's
  // state, or "" (docs/voice-desk.md, "Readiness, Test, and the startup
  // warning on the Mac").
  function macHint(t, state) {
    if (!t || !isMac(t.host) || t.registration !== "none" || t.enrolled_on_two_hosts) return "";
    var link = linkFor(state, t.host);
    return MAC_HINT + (link ? " " + linkLine(link).text + "." : "");
  }

  // The route and body of an enroll or unenroll ("enroll" | "unenroll"):
  // the Pi's own edit, or the host-qualified one, which a phase 1 hub
  // refuses rather than reading it as a Pi edit.
  function enrollRequest(kind, host, sessionId) {
    if (!isMac(host)) return { path: "/desk/api/" + kind, body: { session_id: sessionId } };
    return { path: "/desk/api/" + kind + "-host", body: { session_id: sessionId, host: host } };
  }

  // What the settings note says of an edit's answer; r is {status, data}, or
  // null when no answer came.
  function enrollOutcome(r, kind, host) {
    if (!r) return "Could not reach the hub.";
    if (r.status === 200) return kind === "enroll" ? "Enrolled." : "Unenrolled.";
    if (r.status === 404 && isMac(host)) return OLDER_HUB;
    return "Failed: " + (r.data && r.data.error ? r.data.error : "HTTP " + r.status) + ".";
  }

  // The hosts a target's unenroll buttons name. An id two hosts own routes
  // nowhere, and the snapshot doesn't say which hosts those are, so each
  // host the page knows is offered; an unenroll from a host that doesn't
  // own the id changes nothing.
  function unenrollHosts(t, state) {
    if (t.enrolled_on_two_hosts) return hostChoices(state);
    return t.enrolled ? [t.host] : [];
  }

  function candidates(state) {
    return state && Array.isArray(state.candidates) ? state.candidates : [];
  }

  var api = {
    ENROLL_NOW: ENROLL_NOW, ENROLL_NOW_NOTICE: ENROLL_NOW_NOTICE, OLDER_HUB: OLDER_HUB, MAC_HINT: MAC_HINT,
    hostLabel: hostLabel, isMac: isMac, linkFor: linkFor, linkLine: linkLine, refusalLine: refusalLine,
    refusalLines: refusalLines, hostChoices: hostChoices, macHint: macHint, enrollRequest: enrollRequest,
    enrollOutcome: enrollOutcome, unenrollHosts: unenrollHosts, candidates: candidates
  };
  root.DeskHosts = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
