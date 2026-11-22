// Startup-blocked sessions: the captured development-channel warning and the
// Acknowledge button, which asks the hub to press Enter once in that
// session's pane. The hub sends only if the screen and the process still
// match the digest of what is shown here. Same rules as app.js: no build
// step, text only through textContent, and the token stays in app.js (this
// file gets its `api` function, never the token).
(function () {
  "use strict";

  var POLL_MS = 5000;
  // A sent acknowledgement's confirmation outlives its item (the session
  // leaves the warning at once) for this long.
  var SENT_NOTE_MS = 60000;

  function create(opts) {
    var api = opts.api; // app.js's api(method, path, body) -> Promise<{status, data}>
    var labelFor = opts.labelFor; // session id -> label or null
    var root = document.getElementById("startup");
    var list = document.getElementById("startup-list");
    var run = 0; // bumps on stop() so stale polls do nothing
    var timer = null;
    var shownKey = null;
    var notes = {}; // session id -> {text, tone}, kept across polls
    var pending = {}; // session id -> true while its acknowledgement is in flight
    var sent = null; // {text, until} after a successful acknowledgement
    var sentNode = document.getElementById("startup-sent");

    function el(tag, props, text) {
      var node = document.createElement(tag);
      Object.keys(props || {}).forEach(function (k) {
        if (k === "class") node.className = props[k];
        else node.setAttribute(k, props[k]);
      });
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function render(sessions) {
      var showSent = !!sent && Date.now() < sent.until;
      var key = JSON.stringify([sessions, notes, pending, showSent && sent.text]);
      if (key === shownKey) return;
      shownKey = key;
      while (list.firstChild) list.removeChild(list.firstChild);
      sentNode.hidden = !showSent;
      sentNode.textContent = showSent ? sent.text : "";
      root.hidden = sessions.length === 0 && !showSent;
      sessions.forEach(function (s) {
        var label = labelFor(s.session_id) || s.session_id;
        var item = el("li", { "class": "startup-item", "data-testid": "startup-item", "data-session": s.session_id });
        item.appendChild(el("h3", {}, label + " (" + s.session_id.slice(0, 8) + ")"));
        // The pane's blank rows below the dialog only push the button away.
        var screen = (s.capture || "").replace(/^\s*\n/, "").replace(/\s+$/, "");
        item.appendChild(el("pre", { "class": "screen", "data-testid": "startup-capture" }, screen));
        if (s.refusal) item.appendChild(el("p", { "class": "problem", "data-testid": "startup-refusal" }, s.refusal));
        var btn = el("button", { type: "button", "class": "btn warnbtn", "data-testid": "startup-ack" },
          "Acknowledge startup warning");
        btn.disabled = !s.digest || !!pending[s.session_id];
        btn.addEventListener("click", function () { acknowledge(s); });
        item.appendChild(el("div", { "class": "row" })).appendChild(btn);
        var note = notes[s.session_id];
        if (note) item.appendChild(el("p", { "class": "result", "data-tone": note.tone, "data-testid": "startup-result", role: "status" }, note.text));
        list.appendChild(item);
      });
    }

    function acknowledge(s) {
      var myRun = run;
      pending[s.session_id] = true;
      notes[s.session_id] = { text: "Sending Enter...", tone: "" };
      poll(false);
      api("POST", "/desk/api/startup/ack", { session_id: s.session_id, digest: s.digest }).then(function (r) {
        if (myRun !== run) return;
        delete pending[s.session_id];
        if (r.status === 200) {
          delete notes[s.session_id];
          sent = { text: "Enter sent to " + (labelFor(s.session_id) || s.session_id) + ". The session continues starting.",
                   until: Date.now() + SENT_NOTE_MS };
        } else {
          notes[s.session_id] = { text: "Not sent: " + ((r.data && r.data.error) || "HTTP " + r.status) + ".", tone: "bad" };
        }
        poll(false);
      }, function () {
        if (myRun !== run) return;
        delete pending[s.session_id];
        notes[s.session_id] = { text: "Result unknown: the hub could not be reached.", tone: "warn" };
        poll(false);
      });
    }

    function poll(schedule) {
      var myRun = run;
      api("GET", "/desk/api/startup").then(function (r) {
        if (myRun !== run) return;
        if (r.status === 200 && r.data && Array.isArray(r.data.sessions)) {
          var ids = r.data.sessions.map(function (s) { return s.session_id; });
          // A session that left the warning keeps no stale note.
          Object.keys(notes).forEach(function (id) { if (ids.indexOf(id) < 0 && !pending[id]) delete notes[id]; });
          render(r.data.sessions);
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
        notes = {};
        pending = {};
        sent = null;
        sentNode.hidden = true;
        while (list.firstChild) list.removeChild(list.firstChild);
        root.hidden = true;
      },
      refresh: function () { shownKey = null; }
    };
  }

  window.DeskStartup = { create: create };
})();
