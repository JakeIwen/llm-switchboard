// Startup-blocked sessions: the captured development-channel warning and the
// Acknowledge button, which asks the hub to press Enter once in that
// session's pane. The hub sends only if the screen and the process still
// match the digest of what is shown here. Same rules as app.js: no build
// step, text only through textContent, and the token stays in app.js (this
// file gets its `api` function, never the token).
(function () {
  "use strict";

  var POLL_MS = 5000;
  // An acknowledgement's outcome stays shown this long, even after its
  // session leaves the list (which a sent Enter normally makes it do).
  var OUTCOME_MS = 60000;

  // The three outcomes the page tells apart. Only "unknown" may have typed
  // a key without the hub confirming it; a refusal typed nothing.
  var OUTCOME = {
    sent: function (label) {
      return { tone: "ok", text: "Enter sent to " + label + ". The session continues starting." };
    },
    unknown: function (label, why) {
      return { tone: "warn", text: "Result unknown for " + label + ": " + why +
        " The Enter may have been delivered; check the session before acknowledging again." };
    },
    refused: function (label, why) {
      return { tone: "bad", text: "Not sent to " + label + ": " + why + ". Nothing was typed." };
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
      var shown = liveOutcomes();
      var key = JSON.stringify([sessions, pending, shown]);
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
        var btn = el("button", { type: "button", "class": "btn warnbtn", "data-testid": "startup-ack" },
          pending[s.session_id] ? "Sending Enter..." : "Acknowledge startup warning");
        btn.disabled = !s.digest || !!pending[s.session_id];
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

    function settle(s, outcome) {
      delete pending[s.session_id];
      outcome.until = Date.now() + OUTCOME_MS;
      outcomes[s.session_id] = outcome;
      poll(false);
    }

    function acknowledge(s) {
      var myRun = run;
      var label = labelFor(s.session_id) || s.session_id;
      pending[s.session_id] = true;
      delete outcomes[s.session_id];
      render();
      api("POST", "/desk/api/startup/ack", { session_id: s.session_id, digest: s.digest }).then(function (r) {
        if (myRun !== run) return;
        var why = (r.data && r.data.error) || "HTTP " + r.status;
        if (r.status === 200) settle(s, OUTCOME.sent(label));
        else if (r.data && r.data.delivery === "unknown") settle(s, OUTCOME.unknown(label, "tmux did not confirm the Enter."));
        else settle(s, OUTCOME.refused(label, why));
      }, function () {
        if (myRun !== run) return;
        settle(s, OUTCOME.unknown(label, "the hub could not be reached."));
      });
    }

    function poll(schedule) {
      var myRun = run;
      api("GET", "/desk/api/startup").then(function (r) {
        if (myRun !== run) return;
        if (r.status === 200 && r.data && Array.isArray(r.data.sessions)) {
          sessions = r.data.sessions;
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
        while (list.firstChild) list.removeChild(list.firstChild);
        while (outcomeList.firstChild) outcomeList.removeChild(outcomeList.firstChild);
        root.hidden = true;
      },
      refresh: function () { shownKey = null; }
    };
  }

  window.DeskStartup = { create: create };
})();
