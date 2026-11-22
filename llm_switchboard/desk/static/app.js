// Voice desk tablet page. No build step; CSP is default-src 'self', so no
// inline script or style. Server and user text only ever goes through
// textContent. The bearer token lives only in the closure below.
(function () {
  "use strict";

  var Rule = window.DeskTextRule;
  var TEST_TEXT = 'Desk test: reply with "desk OK" and nothing else.';
  var POLL_ABORT_MS = 35000;
  var CLAIM_EVERY_MS = 2000;

  var token = null; // never stored anywhere else
  var session = 0; // bumps on every (re)pairing so stale loops stop
  var state = null; // last good /state body
  var selectedId = null;
  var primed = false; // first state load must not speak old replies
  var spoken = new Set();
  var recognition = null;
  var clearOpen = false;
  var compose = freshCompose();

  function $(id) { return document.getElementById(id); }

  function freshCompose() {
    // dest is frozen when typing or dictation starts; seq invalidates
    // in-flight ticket responses after a void.
    return { dest: null, ticket: null, ticketPending: false, phase: "edit", seq: (compose ? compose.seq : 0) + 1 };
  }

  function el(tag, props, kids) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      if (k === "text") node.textContent = props[k];
      else if (k === "class") node.className = props[k];
      else node.setAttribute(k, props[k]);
    });
    (kids || []).forEach(function (kid) { node.appendChild(kid); });
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // ---- HTTP ---------------------------------------------------------------

  function NetworkError(message) { this.message = message; }

  // Resolves {status, data}; rejects with NetworkError when no response came.
  // 401 on any protected request drops the token and returns to pairing.
  function api(method, path, body, opts) {
    opts = opts || {};
    var headers = {};
    if (opts.auth !== false && token) headers.Authorization = "Bearer " + token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    var init = { method: method, headers: headers, cache: "no-store", credentials: "omit" };
    if (body !== undefined) init.body = JSON.stringify(body);
    if (opts.signal) init.signal = opts.signal;
    var mySession = session;
    return fetch(path, init).then(function (res) {
      return res.text().then(function (raw) {
        var data = null;
        try { data = raw ? JSON.parse(raw) : null; } catch (e) { data = null; }
        if (res.status === 401 && opts.auth !== false && mySession === session) {
          dropToken("The hub no longer accepts this tablet.");
        }
        return { status: res.status, data: data };
      });
    }, function () {
      throw new NetworkError("network");
    });
  }

  function errorText(r) {
    return (r.data && r.data.error) ? r.data.error : "HTTP " + r.status;
  }

  // ---- Pairing ------------------------------------------------------------

  function toHex(bytes) {
    return Array.prototype.map.call(new Uint8Array(bytes), function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
  }

  function dropToken(reason) {
    token = null;
    state = null;
    session++;
    voidCompose(null);
    stopSpeaking();
    $("desk").hidden = true;
    $("banner-sends").hidden = true;
    $("banner-lock-store").hidden = true;
    setConn("offline", "Not paired");
    startPairing(reason);
  }

  function startPairing(reason) {
    var mySession = ++session;
    $("pairing").hidden = false;
    $("desk").hidden = true;
    $("pair-new").hidden = true;
    $("pair-code").textContent = "...";
    $("pair-code-inline").textContent = "<code>";
    $("pair-status").textContent = reason || "Requesting a code...";
    var secretBytes = new Uint8Array(32);
    crypto.getRandomValues(secretBytes);
    var secret = toHex(secretBytes);
    var enc = new TextEncoder().encode(secret);
    crypto.subtle.digest("SHA-256", enc).then(function (digest) {
      return api("POST", "/desk/pair/start", { secret_hash: toHex(digest) }, { auth: false });
    }).then(function (r) {
      if (mySession !== session) return;
      if (r.status !== 200 || !r.data || !r.data.code) {
        pairProblem(errorText(r));
        return;
      }
      $("pair-code").textContent = r.data.code;
      $("pair-code-inline").textContent = r.data.code;
      $("pair-status").textContent = "Waiting for confirmation on the Pi.";
      var deadline = Date.now() + (r.data.expires_in || 300) * 1000;
      pollClaim(mySession, r.data.pair_id, secret, deadline);
    }).catch(function () {
      if (mySession === session) pairProblem("Cannot reach the hub.");
    });
  }

  function pairProblem(message) {
    $("pair-status").textContent = message;
    $("pair-new").hidden = false;
  }

  function pollClaim(mySession, pairId, secret, deadline) {
    setTimeout(function () {
      if (mySession !== session) return;
      if (Date.now() > deadline + 5000) { pairProblem("The code expired."); return; }
      api("POST", "/desk/pair/claim", { pair_id: pairId, secret: secret }, { auth: false }).then(function (r) {
        if (mySession !== session) return;
        if (r.status === 200 && r.data && r.data.token) {
          token = r.data.token;
          $("pairing").hidden = true;
          $("desk").hidden = false;
          primed = false;
          startStateLoop();
        } else if (r.status === 409) {
          pollClaim(mySession, pairId, secret, deadline);
        } else {
          pairProblem("The code expired or was refused (" + errorText(r) + ").");
        }
      }).catch(function () {
        if (mySession === session) pollClaim(mySession, pairId, secret, deadline);
      });
    }, CLAIM_EVERY_MS);
  }

  // ---- State loop ---------------------------------------------------------

  function setConn(kind, text) {
    var node = $("conn");
    node.setAttribute("data-state", kind);
    node.textContent = text;
  }

  function startStateLoop() {
    var mySession = session;
    var failures = 0;
    setConn("connecting", "Connecting");
    function once() {
      if (mySession !== session || !token) return;
      var ctl = new AbortController();
      var timer = setTimeout(function () { ctl.abort(); }, POLL_ABORT_MS);
      var path = "/desk/api/state" + (state ? "?after=" + encodeURIComponent(state.version) : "");
      api("GET", path, undefined, { signal: ctl.signal }).then(function (r) {
        clearTimeout(timer);
        if (mySession !== session) return;
        if (r.status === 200 && r.data) {
          failures = 0;
          setConn("online", "Connected");
          applyState(r.data);
          once();
        } else {
          retry();
        }
      }, function () {
        clearTimeout(timer);
        retry();
      });
    }
    function retry() {
      if (mySession !== session) return;
      failures++;
      setConn("offline", "Disconnected, retrying");
      if (compose.phase === "edit" || compose.phase === "review") {
        voidCompose("Disconnected: the review was discarded.");
      }
      setTimeout(once, Math.min(10000, 500 * Math.pow(2, failures)));
    }
    once();
  }

  function applyState(next) {
    var epochChanged = state && state.epoch !== next.epoch;
    state = next;
    $("banner-sends").hidden = next.sends_enabled !== false;
    var lockErr = $("banner-lock-store");
    lockErr.hidden = !next.lock_store_error;
    lockErr.textContent = next.lock_store_error
      ? "Lock store error: " + String(next.lock_store_error) + ". Sends are refused."
      : "";
    if (selectedId === null && next.targets.length === 1) selectedId = next.targets[0].session_id;
    if (selectedId !== null && !findTarget(selectedId)) selectedId = null;
    if (epochChanged) voidCompose("The hub restarted: the review was discarded.");
    else validateCompose();
    speakNewReplies(next);
    render();
  }

  function findTarget(id) {
    if (!state) return null;
    for (var i = 0; i < state.targets.length; i++) {
      if (state.targets[i].session_id === id) return state.targets[i];
    }
    return null;
  }

  // ---- Target display -----------------------------------------------------

  function badgeFor(t) {
    if (t.lock) {
      return t.lock.earlier_run
        ? { text: "Unknown (earlier hub run)", tone: "warn" }
        : { text: "Busy", tone: "warn" };
    }
    switch (t.registration) {
      case "verified": return { text: "Verified", tone: "ok" };
      case "registered": return { text: "Registered, no desk reply verified yet", tone: "warn" };
      case "ambiguous": return { text: "Ambiguous", tone: "bad" };
      default: return { text: "Unavailable", tone: "" };
    }
  }

  function canCompose(t) {
    return !!t && !t.lock && (t.registration === "verified" || t.registration === "registered");
  }

  function folderOf(t) { return t.cwd || ""; }

  function render() {
    renderTargets();
    renderDetail();
    renderSettings();
  }

  function renderTargets() {
    var list = $("targets");
    clear(list);
    var targets = state ? state.targets : [];
    $("targets-empty").hidden = targets.length > 0;
    targets.forEach(function (t) {
      var badge = badgeFor(t);
      var btn = el("button", { type: "button", "class": "target", "data-testid": "target", "data-session": t.session_id }, [
        el("span", { "class": "name", text: t.label || t.session_id }),
        el("span", { "class": "folder", text: folderOf(t) }),
        el("span", { "class": "badge", "data-tone": badge.tone, text: badge.text })
      ]);
      if (t.session_id === selectedId) btn.setAttribute("aria-current", "true");
      btn.addEventListener("click", function () { selectTarget(t.session_id); });
      list.appendChild(el("li", {}, [btn]));
    });
  }

  function selectTarget(id) {
    if (id === selectedId) return;
    stopRecognition();
    stopSpeaking();
    voidCompose(null);
    $("draft").value = "";
    $("interim").textContent = "";
    clearOpen = false;
    hideResult();
    selectedId = id;
    render();
  }

  function renderDetail() {
    var t = selectedId === null ? null : findTarget(selectedId);
    $("detail").hidden = !t;
    $("pick-note").hidden = !!t;
    if (!t) return;
    var badge = badgeFor(t);
    $("detail-title").textContent = t.label || t.session_id;
    $("detail-folder").textContent = folderOf(t);
    var b = $("detail-badge");
    b.textContent = badge.text;
    b.setAttribute("data-tone", badge.tone);

    $("clear-area").hidden = !t.lock;
    var showConfirm = !!t.lock && clearOpen;
    $("clear-confirm").hidden = !showConfirm;
    $("clear-btn").hidden = showConfirm;
    if (t.lock) {
      $("clear-text").textContent =
        "Only clear if you checked the session and the earlier request is finished or lost. Lock: request " +
        t.lock.request_id + ", since " + formatTime(t.lock.since) + ".";
    } else {
      clearOpen = false;
    }

    var composable = canCompose(t);
    var editing = compose.phase === "edit";
    var busy = compose.phase === "sending";
    $("draft").disabled = !composable || busy;
    $("dictate").disabled = !composable || busy;
    $("test-btn").disabled = !composable || busy;
    $("review-btn").disabled = !composable || busy;
    $("dictate").textContent = recognition ? "Stop" : "Dictate";
    $("dictate").hidden = !speechInputAvailable();
    if (!composable && editing) {
      $("compose-note").textContent = t.lock
        ? "This target is busy."
        : "This target is not available for sends.";
    }
    renderReview();
    renderReplies(t);
  }

  function formatTime(since) {
    if (typeof since !== "number") return "unknown time";
    var d = new Date(since < 1e12 ? since * 1000 : since);
    return d.toLocaleString();
  }

  // ---- Compose / review ---------------------------------------------------

  function speechInputAvailable() {
    return typeof window.webkitSpeechRecognition === "function" || typeof window.SpeechRecognition === "function";
  }

  function setNote(text) { $("compose-note").textContent = text || ""; }

  function hideResult() {
    var r = $("send-result");
    r.hidden = true;
    r.textContent = "";
  }

  function showResult(text, tone) {
    var r = $("send-result");
    r.hidden = false;
    r.textContent = text;
    r.setAttribute("data-tone", tone || "");
  }

  // Drops the frozen destination and ticket. Keeps the draft text so it can
  // be reviewed again against a fresh destination.
  function voidCompose(reason) {
    stopRecognition();
    compose = freshCompose();
    $("review").hidden = true;
    setNote(reason || "");
  }

  function validateCompose() {
    if (!compose.dest || (compose.phase !== "edit" && compose.phase !== "review")) return;
    var d = compose.dest;
    var t = findTarget(d.session_id);
    if (state.epoch !== d.epoch) return voidCompose("The hub restarted: the review was discarded.");
    if (!t) return voidCompose("The target went away: the review was discarded.");
    if (t.generation !== d.generation) return voidCompose("The target reconnected: the review was discarded.");
    if (!canCompose(t)) return voidCompose("The target changed state: the review was discarded.");
  }

  // Freeze destination and ask for a ticket; the first typed or dictated
  // character, Review, or Test calls this.
  function freezeDestination() {
    if (compose.dest) return true;
    var t = selectedId === null ? null : findTarget(selectedId);
    if (!canCompose(t) || !state) return false;
    var dest = { host: t.host, session_id: t.session_id, generation: t.generation, label: t.label || t.session_id, epoch: state.epoch };
    compose.dest = dest;
    compose.ticketPending = true;
    var seq = compose.seq;
    api("POST", "/desk/api/ticket", { session_id: dest.session_id, generation: dest.generation, epoch: dest.epoch })
      .then(function (r) {
        if (seq !== compose.seq) return;
        compose.ticketPending = false;
        if (r.status === 200 && r.data && r.data.ticket) {
          compose.ticket = r.data.ticket;
        } else {
          voidCompose("No ticket: " + errorText(r));
        }
        render();
      }, function () {
        if (seq !== compose.seq) return;
        voidCompose("No ticket: the hub could not be reached.");
        render();
      });
    return true;
  }

  function startReview(text) {
    if (!freezeDestination()) return;
    compose.phase = "review";
    compose.text = Rule.normalizeText(text);
    hideResult();
    render();
  }

  function renderReview() {
    var panel = $("review");
    var show = compose.dest && (compose.phase === "review" || compose.phase === "sending");
    panel.hidden = !show;
    if (!show) return;
    var dest = $("review-dest");
    clear(dest);
    [["To", compose.dest.label], ["Host", compose.dest.host], ["Session", compose.dest.session_id],
      ["Generation", String(compose.dest.generation)]].forEach(function (pair) {
      dest.appendChild(el("dt", { text: pair[0] }));
      dest.appendChild(el("dd", { text: pair[1] }));
    });
    $("review-text").textContent = compose.text;
    var problem = Rule.checkText(compose.text);
    var p = $("review-problem");
    p.hidden = !problem;
    p.textContent = problem ? "This text cannot be sent: " + problem + "." : "";
    var sending = compose.phase === "sending";
    var ready = !problem && !!compose.ticket && !compose.ticketPending && !!state && state.sends_enabled === true &&
      !state.lock_store_error;
    $("send-btn").disabled = !ready || sending;
    $("send-btn").textContent = compose.ticketPending ? "Getting ticket..." : (sending ? "Sending..." : "Send");
    $("edit-btn").disabled = sending;
    $("discard-btn").disabled = sending;
  }

  function doSend() {
    if (compose.phase !== "review" || !compose.ticket) return;
    var text = compose.text;
    if (Rule.checkText(text)) return;
    var seq = compose.seq;
    var ticket = compose.ticket;
    compose.phase = "sending";
    render();
    api("POST", "/desk/api/send", { ticket: ticket, text: text }).then(function (r) {
      if (seq !== compose.seq || compose.phase !== "sending") return;
      if (r.status === 200) {
        finishSend("Sent. Waiting for the reply.", "ok");
        $("draft").value = "";
      } else {
        // The hub answered with a refusal: it did not dispatch.
        finishSend("Not sent: " + errorText(r) + ".", "bad");
      }
    }, function () {
      if (seq !== compose.seq || compose.phase !== "sending") return;
      // No response arrived: the hub may have handed the text off. Never
      // offer a retry; the hub's state shows whether the target is busy.
      stopRecognition();
      compose = freshCompose();
      compose.phase = "unknown";
      showResult("Result unknown: the connection dropped while sending, so the request may have been delivered. " +
        "Check the target and its replies. It is not retried.", "warn");
      $("review").hidden = true;
      render();
    });
  }

  function finishSend(text, tone) {
    compose = freshCompose();
    compose.phase = "done";
    $("review").hidden = true;
    showResult(text, tone);
    render();
  }

  function resetAfterResult() {
    if (compose.phase === "done" || compose.phase === "unknown") {
      compose = freshCompose();
      hideResult();
    }
  }

  // ---- Dictation ----------------------------------------------------------

  function stopRecognition() {
    if (!recognition) return;
    var r = recognition;
    recognition = null;
    r.onresult = r.onend = r.onerror = null;
    try { r.abort(); } catch (e) { /* already stopped */ }
    $("interim").textContent = "";
    $("dictate").textContent = "Dictate";
  }

  function toggleDictation() {
    if (recognition) { stopRecognition(); return; }
    resetAfterResult();
    if (!freezeDestination()) { setNote("This target is not available for sends."); return; }
    var Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
    var rec = new Ctor();
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.continuous = false;
    rec.onresult = function (e) {
      var finalText = "";
      var interim = "";
      for (var i = e.resultIndex; i < e.results.length; i++) {
        var piece = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += piece; else interim += piece;
      }
      if (finalText) {
        var draft = $("draft");
        var sep = draft.value && !/\s$/.test(draft.value) ? " " : "";
        draft.value += sep + finalText.trim();
      }
      $("interim").textContent = interim;
    };
    rec.onerror = function (e) {
      if (rec !== recognition) return;
      setNote("Dictation stopped (" + (e && e.error ? e.error : "error") + "). You can type instead.");
    };
    rec.onend = function () {
      if (rec !== recognition) return;
      recognition = null;
      $("interim").textContent = "";
      $("dictate").textContent = "Dictate";
    };
    recognition = rec;
    try {
      rec.start();
    } catch (e) {
      recognition = null;
      setNote("Dictation could not start. You can type instead.");
    }
    $("dictate").textContent = "Stop";
  }

  // ---- Speech output ------------------------------------------------------

  function voiceAvailable() {
    return "speechSynthesis" in window && window.speechSynthesis.getVoices().length > 0;
  }

  function stopSpeaking() {
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  }

  function speak(text) {
    if (!voiceAvailable() || !text) return;
    window.speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US";
    window.speechSynthesis.speak(u);
  }

  // Speaks a reply only when it newly arrives for the target on screen.
  function speakNewReplies(next) {
    next.requests.forEach(function (rq) {
      if (!rq.reply || spoken.has(rq.request_id)) return;
      spoken.add(rq.request_id);
      if (primed && rq.session_id === selectedId && document.visibilityState === "visible") {
        speak(rq.reply.spoken);
      }
    });
    primed = true;
  }

  function renderReplies(t) {
    var list = $("replies");
    clear(list);
    var mine = state.requests.filter(function (rq) { return rq.session_id === t.session_id; });
    mine.reverse();
    if (!mine.length) {
      list.appendChild(el("li", { "class": "note", text: "No requests yet." }));
      return;
    }
    mine.forEach(function (rq) {
      var li = el("li", { "data-testid": "request", "data-request": rq.request_id }, [
        el("div", { "class": "reply-meta", text: describeState(rq) }),
        el("div", { "class": "reply-text sent", text: rq.text })
      ]);
      if (rq.reply) {
        li.appendChild(el("div", { "class": "reply-text", "data-testid": "reply-text", text: rq.reply.text }));
        var play = el("button", { type: "button", "class": "btn quiet", "data-testid": "play", text: "Play" });
        play.addEventListener("click", function () { speak(rq.reply.spoken); });
        var row = el("div", { "class": "row" }, [play]);
        if (!voiceAvailable()) row.appendChild(el("span", { "class": "note", text: "No speech voice on this device" }));
        li.appendChild(row);
      }
      list.appendChild(li);
    });
  }

  function describeState(rq) {
    var map = {
      pending: "Accepted, not yet delivered",
      handed_off: "Delivered, waiting for the reply",
      answered: "Answered",
      unknown: "Result unknown (connection lost after delivery)",
      expired: "Not delivered (safe to send again)"
    };
    return (map[rq.state] || rq.state) + " - " + formatTime(rq.created);
  }

  // ---- Clear, enroll ------------------------------------------------------

  function doClear() {
    var t = selectedId === null ? null : findTarget(selectedId);
    if (!t || !t.lock) return;
    api("POST", "/desk/api/clear", { session_id: t.session_id, lock_id: t.lock.lock_id }).then(function (r) {
      clearOpen = false;
      if (r.status === 200) hideResult();
      else showResult("Not cleared: " + errorText(r) + ".", "bad");
      render();
    }, function () {
      showResult("Clear result unknown: the hub could not be reached.", "warn");
    });
  }

  function enrollCall(kind, id) {
    var note = $("settings-note");
    api("POST", "/desk/api/" + kind, { session_id: id }).then(function (r) {
      note.textContent = r.status === 200 ? (kind === "enroll" ? "Enrolled." : "Unenrolled.") : "Failed: " + errorText(r) + ".";
    }, function () {
      note.textContent = "Could not reach the hub.";
    });
  }

  function renderSettings() {
    var list = $("enroll-list");
    clear(list);
    (state ? state.targets : []).forEach(function (t) {
      var kind = t.enrolled ? "unenroll" : "enroll";
      var btn = el("button", { type: "button", "class": "btn", "data-testid": kind + "-btn", text: t.enrolled ? "Unenroll" : "Enroll" });
      btn.addEventListener("click", function () { enrollCall(kind, t.session_id); });
      list.appendChild(el("li", {}, [el("span", { text: (t.label || t.session_id) + " (" + t.session_id + ")" }), btn]));
    });
  }

  // ---- Wiring -------------------------------------------------------------

  function wire() {
    $("pair-new").addEventListener("click", function () { startPairing(); });
    $("settings-toggle").addEventListener("click", function () {
      var open = $("settings").hidden;
      $("settings").hidden = !open;
      $("settings-toggle").setAttribute("aria-expanded", String(open));
    });
    $("enroll-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var id = $("enroll-input").value.trim();
      if (!id || !token) return;
      enrollCall("enroll", id);
      $("enroll-input").value = "";
    });
    $("draft").addEventListener("input", function () {
      resetAfterResult();
      if (compose.phase === "review") { compose.phase = "edit"; $("review").hidden = true; }
      freezeDestination();
      render();
    });
    $("review-btn").addEventListener("click", function () { resetAfterResult(); startReview($("draft").value); });
    $("test-btn").addEventListener("click", function () {
      resetAfterResult();
      voidCompose(null);
      $("draft").value = TEST_TEXT;
      startReview(TEST_TEXT);
    });
    $("dictate").addEventListener("click", toggleDictation);
    $("send-btn").addEventListener("click", doSend);
    $("edit-btn").addEventListener("click", function () {
      if (compose.phase === "review") { compose.phase = "edit"; render(); }
    });
    $("discard-btn").addEventListener("click", function () {
      voidCompose("Review discarded.");
      render();
    });
    $("clear-btn").addEventListener("click", function () { clearOpen = true; render(); });
    $("clear-no").addEventListener("click", function () { clearOpen = false; render(); });
    $("clear-yes").addEventListener("click", doClear);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState !== "hidden") return;
      stopRecognition();
      stopSpeaking();
      if (compose.phase === "edit" || compose.phase === "review") {
        voidCompose("The page was hidden: the review and ticket were discarded.");
        render();
      }
    });
    if ("speechSynthesis" in window) {
      window.speechSynthesis.addEventListener("voiceschanged", function () { if (state) render(); });
    }
    window.addEventListener("pagehide", stopSpeaking);
  }

  wire();
  startPairing();
})();
