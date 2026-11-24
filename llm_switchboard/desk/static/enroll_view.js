// Enrollment on the tablet page (docs/voice-desk.md, "Enrollment for Mac
// sessions", "What the desk page shows"): the settings list with each
// target's host and its enroll or unenroll, the typed form's host choice,
// and the Mac sessions asking to join, each with Enroll now. Every Mac
// enroll, from a candidate or the form, waits for the Enroll now
// confirmation: enrolling a running Mac session takes effect at once. Every
// edit goes through the page's one `api` (hub_api.js), keyed by
// (host, session_id). Text only ever goes through textContent.
(function (root) {
  "use strict";

  var Hosts = root.DeskHosts || require("./desk_hosts.js");
  var Logic = root.DeskLogic || require("./desk_logic.js");

  // o: {doc: the page's document, api: the page's api, hostBadge(host): an
  // element (link_view.js), paired(): whether the page holds a token}.
  function create(o) {
    var doc = o.doc;
    function $(id) { return doc.getElementById(id); }
    var state = null;
    // The Enroll now confirmation on screen: {host, session_id, label, from:
    // "candidate" | "form"}, or null.
    var pending = null;
    var era = 0; // bumps on reset, so an answer from before it is dropped

    function el(tag, props, kids) {
      var node = doc.createElement(tag);
      Object.keys(props || {}).forEach(function (k) {
        if (k === "text") node.textContent = props[k];
        else if (k === "class") node.className = props[k];
        else node.setAttribute(k, props[k]);
      });
      (kids || []).forEach(function (kid) { node.appendChild(kid); });
      return node;
    }

    function empty(node) { while (node.firstChild) node.removeChild(node.firstChild); }

    function button(testid, text, onTap, cls) {
      var b = el("button", { type: "button", "class": cls || "btn", "data-testid": testid, text: text });
      b.addEventListener("click", onTap);
      return b;
    }

    // noteId: where the answer is said, beside the control that asked.
    function edit(kind, host, sessionId, noteId) {
      var mine = era;
      var req = Hosts.enrollRequest(kind, host, sessionId);
      function said(r) {
        if (mine !== era) return;
        $(noteId).textContent = Hosts.enrollOutcome(r, kind, host);
        render(state);
      }
      o.api("POST", req.path, req.body).then(said, function () { said(null); });
    }

    // An enroll: the Pi's at once, a Mac's after its confirmation.
    function enroll(host, sessionId, label, from) {
      if (!Hosts.isMac(host)) return edit("enroll", host, sessionId, "settings-note");
      pending = { host: host, session_id: sessionId, label: label || sessionId, from: from };
      render(state);
    }

    function confirmed() {
      var p = pending;
      pending = null;
      if (p) edit("enroll", p.host, p.session_id, p.from === "candidate" ? "candidates-note" : "settings-note");
      render(state);
    }

    function confirmBox() {
      return el("div", { "class": "confirm", "data-testid": "enroll-now-confirm" }, [
        el("p", { "data-testid": "enroll-now-text", text: Hosts.ENROLL_NOW_NOTICE }),
        el("p", { "class": "note", text: pending.label + " (" + Hosts.hostLabel(pending.host) + ", " +
          pending.session_id + ")" }),
        el("div", { "class": "row" }, [
          button("enroll-now-yes", Hosts.ENROLL_NOW, confirmed, "btn warnbtn"),
          button("enroll-now-no", "Cancel", function () { pending = null; render(state); }, "btn quiet")
        ])
      ]);
    }

    function renderTargets(list) {
      (state ? state.targets : []).forEach(function (t) {
        var label = (t.label || t.session_id) + " (" + t.session_id + ")";
        var row = el("li", { "data-host": t.host, "data-session": t.session_id }, [o.hostBadge(t.host),
          el("span", { text: label })]);
        var from = Hosts.unenrollHosts(t, state);
        from.forEach(function (host) {
          var text = from.length > 1 ? "Unenroll on " + Hosts.hostLabel(host) : "Unenroll";
          row.appendChild(button("unenroll-btn", text, function () { edit("unenroll", host, t.session_id, "settings-note"); }));
        });
        if (!from.length) {
          row.appendChild(button("enroll-btn", Hosts.isMac(t.host) ? Hosts.ENROLL_NOW : "Enroll", function () {
            enroll(t.host, t.session_id, t.label, "form");
          }));
        }
        list.appendChild(row);
      });
    }

    function renderCandidates() {
      var list = $("candidate-list");
      var shown = Hosts.candidates(state);
      shown.forEach(function (c) {
        var item = el("li", { "data-testid": "candidate", "data-host": c.host, "data-session": c.session_id }, [
          o.hostBadge(c.host),
          el("span", { "class": "name", text: (c.label || c.session_id) + " (" + c.session_id.slice(0, 8) + ")" }),
          el("span", { "class": "folder", text: c.folder || "" })
        ]);
        if (pending && pending.from === "candidate" && Logic.sameTarget(pending, c)) item.appendChild(confirmBox());
        else item.appendChild(button("enroll-now", Hosts.ENROLL_NOW, function () {
          enroll(c.host, c.session_id, c.label, "candidate");
        }, "btn primary"));
        list.appendChild(item);
      });
      // Kept up while its note says how the last Enroll now went, though
      // that candidate has left the list.
      $("candidates").hidden = !shown.length && !$("candidates-note").textContent;
    }

    // The typed form's host choice: `pi` and the hosts in `links`, keeping
    // the one chosen while it is still offered.
    function renderHostChoice() {
      var select = $("enroll-host");
      var chosen = select.value;
      var choices = Hosts.hostChoices(state);
      empty(select);
      choices.forEach(function (host) { select.appendChild(el("option", { value: host, text: Hosts.hostLabel(host) })); });
      select.value = choices.indexOf(chosen) === -1 ? Logic.PI : chosen;
    }

    function render(next) {
      state = next;
      // A candidate's confirmation lasts only while the hub still lists it.
      if (pending && pending.from === "candidate" &&
          !Hosts.candidates(state).some(function (c) { return Logic.sameTarget(c, pending); })) pending = null;
      empty($("enroll-list"));
      empty($("candidate-list"));
      empty($("enroll-form-confirm"));
      renderTargets($("enroll-list"));
      renderCandidates();
      renderHostChoice();
      if (pending && pending.from === "form") $("enroll-form-confirm").appendChild(confirmBox());
    }

    function submitted() {
      var id = $("enroll-input").value.trim();
      if (!id || !o.paired()) return;
      enroll($("enroll-host").value || Logic.PI, id, null, "form");
      $("enroll-input").value = "";
    }

    // A new pairing starts clean: no confirmation, note or answer from before.
    function reset() {
      era++;
      pending = null;
      $("settings-note").textContent = "";
      $("candidates-note").textContent = "";
      render(null);
    }

    $("enroll-form").addEventListener("submit", function (e) { e.preventDefault(); submitted(); });
    return { render: render, reset: reset };
  }

  root.DeskEnrollView = { create: create };
})(typeof window !== "undefined" ? window : globalThis);
