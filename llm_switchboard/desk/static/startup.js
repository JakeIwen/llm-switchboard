// Startup-blocked sessions: the captured development-channel warning and the
// Acknowledge button, which asks the hub to press Enter once in that
// session's pane. The hub sends only if the screen and the process still
// match the digest of what is shown here. Same rules as app.js: no build
// step, text only through textContent, and the token stays in app.js (this
// file gets its `api` function, never the token).
(function () {
  "use strict";

  var POLL_MS = 5000;
  // An acknowledgement's outcome stays shown this long from when it is first
  // drawn, even after its session leaves the list (which a sent Enter
  // normally makes it do).
  var OUTCOME_MS = 60000;
  // Longer than the hub's own bound on an acknowledgement (its tmux calls
  // time out after 3 s each); past it the outcome is unknown.
  var ACK_TIMEOUT_MS = 20000;

  // The three outcomes the page tells apart. "sent" only when the hub
  // confirmed the key; "unknown" may have typed it; a refusal typed nothing
  // this time (its reason may say an earlier attempt did).
  var OUTCOME = {
    sent: function (label) {
      return { tone: "ok", text: "Enter sent to " + label + ". The session continues starting.", typed: true };
    },
    unknown: function (label, why) {
      return { tone: "warn", typed: true, text: "Result unknown for " + label + ": " + why +
        " The Enter may have been delivered; check the session before acknowledging again." };
    },
    refused: function (label, why) {
      return { tone: "bad", typed: false, text: "Not sent to " + label + ": " + why + ". Nothing was typed this time." };
    }
  };

  function create(opts) {
    var api = opts.api; // app.js's api(method, path, body) -> Promise<{status, data}>
    var labelFor = opts.labelFor; // session id -> label or null
    var root = document.getElementById("startup");
    var list = document.getElementById("startup-list");
    var outcomeList = document.getElementById("startup-outcomes");
    var run = 0; // bumps on stop() so stale polls do nothing
    var timer = null;
    var shownKey = null;
    var sessions = []; // the last listing
    var pending = {}; // session id -> true while its acknowledgement is in flight
    var outcomes = {}; // session id -> {text, tone, until}, kept across polls and the session leaving
    // Session id -> the digest of a view whose acknowledgement may have typed
    // a key (sent or unknown): its button stays disabled until a listing
    // shows that session with another view.
    var attempted = {};

    function el(tag, props, text) {
      var node = document.createElement(tag);
      Object.keys(props || {}).forEach(function (k) {
        if (k === "class") node.className = props[k];
        else node.setAttribute(k, props[k]);
      });
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function liveOutcomes() {
      var now = Date.now();
      Object.keys(outcomes).forEach(function (id) { if (outcomes[id].until <= now) delete outcomes[id]; });
      return Object.keys(outcomes).sort().map(function (id) { return outcomes[id]; });
    }

    function render() {
      // An outcome's minute starts when it is first drawn, which is now.
      var now = Date.now();
      Object.keys(outcomes).forEach(function (id) {
        if (outcomes[id].until === undefined) outcomes[id].until = now + OUTCOME_MS;
      });
      var shown = liveOutcomes();
      var key = JSON.stringify([sessions, pending, attempted, shown]);
      if (key === shownKey) return;
      shownKey = key;
      while (list.firstChild) list.removeChild(list.firstChild);
      while (outcomeList.firstChild) outcomeList.removeChild(outcomeList.firstChild);
      root.hidden = sessions.length === 0 && shown.length === 0;
      sessions.forEach(function (s) {
        var label = labelFor(s.session_id) || s.session_id;
        var item = el("li", { "class": "startup-item", "data-testid": "startup-item", "data-session": s.session_id });
        item.appendChild(el("h3", {}, label + " (" + s.session_id.slice(0, 8) + ")"));
        // The pane's blank rows below the dialog only push the button away.
        var screen = (s.capture || "").replace(/^\s*\n/, "").replace(/\s+$/, "");
        item.appendChild(el("pre", { "class": "screen", "data-testid": "startup-capture" }, screen));
        if (s.refusal) item.appendChild(el("p", { "class": "problem", "data-testid": "startup-refusal" }, s.refusal));
        var tried = !!s.digest && attempted[s.session_id] === s.digest;
        var btn = el("button", { type: "button", "class": "btn warnbtn", "data-testid": "startup-ack" },
          pending[s.session_id] ? "Sending Enter..." : tried ? "Attempted for this view" : "Acknowledge startup warning");
        btn.disabled = !s.digest || !!pending[s.session_id] || tried;
        btn.addEventListener("click", function () {
          // Disabled before anything else runs, so a second tap can't queue another request.
          if (btn.disabled || pending[s.session_id]) return;
          btn.disabled = true;
          acknowledge(s);
        });
        item.appendChild(el("div", { "class": "row" })).appendChild(btn);
        list.appendChild(item);
      });
      shown.forEach(function (o) {
        outcomeList.appendChild(el("li", { "class": "result", "data-tone": o.tone, "data-testid": "startup-outcome" }, o.text));
      });
    }

    // Drawn at once, never waiting on the next listing, which may not come
    // (the connection can drop right after the POST); the listing that
    // follows only updates the sessions.
    function settle(s, outcome) {
      delete pending[s.session_id];
      if (outcome.typed) attempted[s.session_id] = s.digest;
      outcomes[s.session_id] = { tone: outcome.tone, text: outcome.text };
      render();
      poll(false);
    }

    function acknowledge(s) {
      var myRun = run;
      var label = labelFor(s.session_id) || s.session_id;
      pending[s.session_id] = true;
      delete outcomes[s.session_id];
      render();
      var abort = typeof AbortController === "function" ? new AbortController() : null;
      var timeout = abort ? setTimeout(function () { abort.abort(); }, ACK_TIMEOUT_MS) : null;
      api("POST", "/desk/api/startup/ack", { session_id: s.session_id, digest: s.digest },
          abort ? { signal: abort.signal } : undefined).then(function (r) {
        if (timeout) clearTimeout(timeout);
        if (myRun !== run) return;
        var why = (r.data && r.data.error) || "HTTP " + r.status;
        if (r.status === 200 && r.data && r.data.sent === true) settle(s, OUTCOME.sent(label));
        else if (r.status === 200 || (r.data && r.data.delivery === "unknown")) settle(s, OUTCOME.unknown(label, "tmux did not confirm the Enter."));
        else settle(s, OUTCOME.refused(label, why));
      }, function () {
        if (timeout) clearTimeout(timeout);
        if (myRun !== run) return;
        // The request may have reached the hub before the connection failed.
        settle(s, OUTCOME.unknown(label, "the hub could not be reached or did not answer."));
      });
    }

    // A local attempt is cleared once the listing shows its session with
    // another view (or not at all); until then that view's button stays off.
    // A listed session with no digest (its pane briefly couldn't be
    // inspected) is no evidence of another view: the attempt stays, so the
    // same view coming back keeps its button off.
    function dropStaleAttempts() {
      var current = {};
      sessions.forEach(function (s) { current[s.session_id] = s.digest; });
      Object.keys(attempted).forEach(function (id) {
        var listed = Object.prototype.hasOwnProperty.call(current, id);
        if (!listed || (current[id] != null && current[id] !== attempted[id])) delete attempted[id];
      });
    }

    function poll(schedule) {
      var myRun = run;
      api("GET", "/desk/api/startup").then(function (r) {
        if (myRun !== run) return;
        if (r.status === 200 && r.data && Array.isArray(r.data.sessions)) {
          sessions = r.data.sessions;
          dropStaleAttempts();
          render();
        }
      }, function () { /* the state loop reports the connection */ }).then(function () {
        if (schedule && myRun === run) timer = setTimeout(function () { poll(true); }, POLL_MS);
      });
    }

    return {
      start: function () {
        this.stop();
        poll(true);
      },
      stop: function () {
        run++;
        if (timer) clearTimeout(timer);
        timer = null;
        shownKey = null;
        sessions = [];
        pending = {};
        outcomes = {};
        attempted = {};
        while (list.firstChild) list.removeChild(list.firstChild);
        while (outcomeList.firstChild) outcomeList.removeChild(outcomeList.firstChild);
        root.hidden = true;
      },
      refresh: function () { shownKey = null; }
    };
  }

  window.DeskStartup = { create: create };
})();
