// Voice desk tablet page. No build step; CSP is default-src 'self', so no
// inline script or style. Server and user text only ever goes through
// textContent. The bearer token lives only in the closure below.
(function () {
  "use strict";

  var Rule = window.DeskTextRule;
  var Logic = window.DeskLogic;
  var TEST_TEXT = 'Desk test: reply with "desk OK" and nothing else.';
  var POLL_ABORT_MS = 35000;
  var SEND_DEADLINE_MS = 20000;
  var BUSY_NOTE = "This target is busy.";
  var CLAIM_EVERY_MS = 2000;

  var token = null; // never stored anywhere else
  var session = 0; // bumps on every (re)pairing so stale loops stop
  var state = null; // last good /state body
  var selectedId = null;
  var clearFrozen = null; // {session_id, lock_id, request_id} while the confirmation is open
  var sentRequestId = null; // request whose "waiting" result is on screen
  var pollSeq = 0; // counts state requests as they start
  var sentAckSeq = null; // pollSeq when the send was acknowledged
  var sentAckVersion = null; // version of the fresh snapshot fetched after the send ack
  var compose = freshCompose();
  // Tapped send POSTs not yet settled, counted apart from `compose`, which a
  // target change replaces while the POST is still out: Start waits for them.
  var tapSends = 0;
  var startup = null; // startup.js's blocked-session view, created in wire()
  var conv = null, convView = null; // conversation.js's controller and conversation_view.js, created in wire()
  var audio = null; // page_audio.js: reply speech, Play and one-shot Dictate, created in wire()

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

  // hub_api.js: every call the page makes, resolving {status, data}.
  var api = window.DeskHubApi.create({
    token: function () { return token; }, session: function () { return session; }, onStatus: statusArrived,
    onBodyLost: bodyLost
  });

  // Every status line the hub sends, on any call, the moment it arrives:
  // first the conversation's gate (conversation.js, hubStatus), whichever
  // call it answers and whether or not its caller still wants it; then a
  // 401 on any protected request drops the token and returns to pairing,
  // unless the page has paired again since the call began.
  function statusArrived(status, opts, startedIn) {
    try { conv.hubStatus(status, opts.call); } catch (e) { pageFault(e); }
    if (status === 401 && opts.auth !== false && startedIn === session) {
      dropToken("The hub no longer accepts this tablet.");
    }
  }

  // A 200 whose body never arrived, on any call: the conversation's one
  // entry for it (conversation.js, bodyLost), before the caller's own code
  // and whether or not that caller still wants the answer.
  function bodyLost() {
    try { conv.bodyLost(); } catch (e) { pageFault(e); }
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
    batch(function () {
      conv.unpaired();
      voidCompose(null);
      audio.stopSpeaking();
      // A new pairing starts clean: no old draft, target, result or settings.
      selectedId = null;
      clearFrozen = null;
      sentRequestId = null;
      audio.forgetReplies();
      $("draft").value = "";
      $("interim").textContent = "";
      hideResult();
      clear($("enroll-list"));
      $("settings-note").textContent = "";
      $("desk").hidden = true;
      startup.stop();
      $("banner-sends").hidden = true;
      $("banner-lock-store").hidden = true;
      // Nothing the hub sent stays in the page, hidden or not.
      ["detail-title", "detail-folder", "detail-badge", "clear-text", "review-dest", "review-text", "replies",
        "banner-lock-store"]
        .forEach(function (id) { $(id).textContent = ""; });
    });
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

  // The hub refuses a claim from a page of another build (a tab from before an install).
  function pageBuild() {
    var meta = document.querySelector('meta[name="desk-build"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function pairProblem(message) {
    $("pair-status").textContent = message;
    $("pair-new").hidden = false;
  }

  function pollClaim(mySession, pairId, secret, deadline) {
    setTimeout(function () {
      if (mySession !== session) return;
      if (Date.now() > deadline + 5000) { pairProblem("The code expired."); return; }
      var claim = { pair_id: pairId, secret: secret, build: pageBuild() };
      api("POST", "/desk/pair/claim", claim, { auth: false }).then(function (r) {
        if (mySession !== session) return;
        if (r.status === 200 && r.data && r.data.token) {
          token = r.data.token;
          $("pairing").hidden = true;
          $("desk").hidden = false;
          audio.forgetReplies();
          startStateLoop();
          startup.start();
        } else if (r.status === 409 && r.data && r.data.error === "not confirmed") {
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
      var startSeq = ++pollSeq;
      var path = "/desk/api/state" + (state ? "?after=" + encodeURIComponent(state.version) : "");
      api("GET", path, undefined, { signal: ctl.signal }).then(function (r) {
        clearTimeout(timer);
        if (mySession !== session) return snapshotAside(r, mySession);
        if (r.status === 200 && r.data) {
          failures = 0;
          setConn("online", "Connected");
          try { applyState(r.data, startSeq); } catch (e) { pageFault(e); }
          once();
        } else {
          // Any other status was the gate's, at its status line, and a lost
          // body bodyLost's; a 200 whose body holds no snapshot silences here.
          if (r.status === 200) conv.snapshot(null);
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

  // A 200 to a state request outside the poll's own path (the tap flow's
  // refresh, an earlier pairing's poll) still reaches the controller, which
  // takes one from an earlier pairing only for what it silences; the page's
  // own state never takes it.
  function snapshotAside(r, askedIn) {
    if (r.status !== 200) return;
    var from = window.DeskConversation.SNAPSHOT_FROM;
    try { conv.snapshot(r.data, askedIn === session ? from.FETCHED : from.EARLIER_PAIRING); } catch (e) { pageFault(e); }
  }

  // A fault in the page's own handling of a snapshot is shown, and polling
  // goes on: the next poll waits on the version already stored, so a fault
  // that repeats can't spin, and the next good snapshot shows Connected again.
  function pageFault(e) {
    setConn("fault", "Page error; reload if this stays");
    if (window.console) window.console.error(e);
  }

  // Everything that depends on the snapshot is reconciled before anything
  // can render it; the speech and the controller's snapshot come after, and
  // the renders they ask for wait for the batch's one render.
  function applyState(next, startSeq) {
    batch(function () {
      runAll([
        function () { reconcile(next, startSeq); },
        function () { audio.repliesArrived(next, selectedId); },
        function () { conv.snapshot(next); },
        function () { audio.pruneReplies(next.requests); }
      ]);
    });
  }

  // The page state the snapshot decides. Renders nothing, speaks nothing.
  function reconcile(next, startSeq) {
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
    expireWaitingResult(next, startSeq);
    if (clearFrozen && !Logic.clearStillValid(clearFrozen, findTarget(clearFrozen.session_id))) clearFrozen = null;
  }

  // "Delivered, waiting" must not outlive the wait.
  function expireWaitingResult(next, startSeq) {
    if (!sentRequestId) return;
    // Gone from history (aged out before this tab saw the end) is as final as answered.
    if (Logic.waitingOver(sentRequestId, next, sentAckVersion, startSeq, sentAckSeq)) {
      sentAckVersion = null;
      sentAckSeq = null;
      sentRequestId = null;
      if (compose.phase === "done") compose = freshCompose();
      hideResult();
    }
  }

  function findTarget(id) {
    if (!state) return null;
    for (var i = 0; i < state.targets.length; i++) {
      if (state.targets[i].session_id === id) return state.targets[i];
    }
    return null;
  }

  // ---- Target display -----------------------------------------------------

  function folderOf(t) { return t.cwd || ""; }

  // ---- Rendering ----------------------------------------------------------
  //
  // A render shows one consistent page. While a batch changes several pieces
  // of page state (a snapshot applied, a target selected, the token dropped),
  // a render asked for in between, such as the controller's onChange from
  // track() or snapshot(), waits, and the batch renders once at its end.
  var held = 0;

  function batch(fn) {
    held++;
    try { fn(); } finally {
      held--;
      if (!held) render();
    }
  }

  // Every step runs even when an earlier one throws, so one fault can't keep
  // the rest (the controller's snapshot above all) from running; the first
  // fault is thrown after.
  function runAll(steps) {
    var fault = null;
    steps.forEach(function (step) { try { step(); } catch (e) { if (!fault) fault = e; } });
    if (fault) throw fault;
  }

  function render() {
    if (held) return;
    runAll([renderTargets, renderDetail, renderSettings, function () { convView.render(conv.view(), startBlocker()); }]);
  }

  // The controller's onChange and page_audio's changed: a fault in the
  // page's render is the page's, shown like a snapshot fault, and never
  // thrown into the steps that asked for it (track() before a speak(), a
  // fetch's answer, a dictation stopped on the way to a send's result).
  function renderForController() {
    try { render(); } catch (e) { pageFault(e); }
  }

  function renderTargets() {
    var list = $("targets");
    clear(list);
    var targets = state ? state.targets : [];
    $("targets-empty").hidden = targets.length > 0;
    targets.forEach(function (t) {
      var badge = Logic.badgeFor(t);
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
    batch(function () {
      conv.targetSelected(id);
      audio.stopDictation();
      audio.stopSpeaking();
      voidCompose(null);
      $("draft").value = "";
      $("interim").textContent = "";
      clearFrozen = null;
      sentRequestId = null;
      hideResult();
      selectedId = id;
    });
  }

  function renderDetail() {
    var t = selectedId === null ? null : findTarget(selectedId);
    $("detail").hidden = !t;
    $("pick-note").hidden = !!t;
    if (!t) return;
    var badge = Logic.badgeFor(t);
    $("detail-title").textContent = t.label || t.session_id;
    $("detail-folder").textContent = folderOf(t);
    var b = $("detail-badge");
    b.textContent = badge.text;
    b.setAttribute("data-tone", badge.tone);

    $("clear-area").hidden = !t.lock;
    // Shown only while the target still shows the lock it names, whatever
    // the order the page's state was updated in.
    var confirm = Logic.clearStillValid(clearFrozen, t) ? clearFrozen : null;
    $("clear-confirm").hidden = !confirm;
    $("clear-btn").hidden = !!confirm;
    if (confirm) {
      $("clear-text").textContent =
        "Only clear if you checked the session and the earlier request is finished or lost. Lock " +
        confirm.lock_id + " for request " + confirm.request_id + ", since " +
        formatTime(t.lock.since) + ".";
    }

    var composable = Logic.canCompose(t);
    var editing = compose.phase === "edit";
    var busy = compose.phase === "sending" || conv.holds();
    // While Dictate listens only its words may change the draft (Test
    // rewrites it), so a late sound can undo them without losing typing.
    var dictating = audio.dictating();
    $("draft").disabled = !composable || busy || dictating;
    // A running dictation keeps its Stop; one that can't start says why.
    var unheard = dictating ? null : audio.dictateBlocker();
    $("dictate").disabled = !composable || busy || !!unheard;
    $("dictate-why").hidden = !unheard || !composable || !audio.speechInputAvailable();
    $("dictate-why").textContent = unheard ? "Dictate is unavailable: " + unheard + "." : "";
    $("test-btn").disabled = !composable || busy || dictating;
    $("review-btn").disabled = !composable || busy;
    $("dictate").textContent = dictating ? "Stop" : "Dictate";
    $("dictate").hidden = !audio.speechInputAvailable();
    var noteNode = $("compose-note");
    if (t.lock) {
      if (!noteNode.textContent) noteNode.textContent = BUSY_NOTE;
    } else if (noteNode.textContent === BUSY_NOTE) {
      noteNode.textContent = "";
    }
    if (!composable && !t.lock && editing) noteNode.textContent = "This target is not available for sends.";
    renderReview();
    renderReplies(t);
  }

  function formatTime(since) {
    if (typeof since !== "number") return "unknown time";
    var d = new Date(since < 1e12 ? since * 1000 : since);
    return d.toLocaleString();
  }

  // ---- Compose / review ---------------------------------------------------

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
    audio.stopDictation();
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
    if (!Logic.canCompose(t)) return voidCompose("The target changed state: the review was discarded.");
  }

  // Freeze destination and ask for a ticket; the first typed or dictated
  // character, Review, or Test calls this.
  function freezeDestination() {
    if (compose.dest) return true;
    var t = selectedId === null ? null : findTarget(selectedId);
    if (!Logic.canCompose(t) || !state) return false;
    var dest = Logic.destinationOf(t, state.epoch);
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
    // Dictation after this point would change the draft but not the review.
    audio.stopDictation();
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
    $("send-btn").disabled = !ready || sending || conv.holds();
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
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, SEND_DEADLINE_MS);
    tapSends++;
    function settled(r) {
      clearTimeout(timer);
      tapSends--;
      if (seq !== compose.seq || compose.phase !== "sending") return render();
      settleSend(Logic.classifySend(r));
    }
    api("POST", "/desk/api/send", { ticket: ticket, text: text }, { signal: ctl.signal })
      .then(settled, function () { settled(null); });
  }

  // A fresh snapshot (no `after`) so a request evicted before this tab's next
  // poll still settles the banner.
  function refreshAfterSend(requestId) {
    var mySession = session;
    pollSeq++;
    api("GET", "/desk/api/state", undefined, {}).then(function (r) {
      // Whatever became of the send it follows, the controller hears it.
      snapshotAside(r, mySession);
      if (mySession !== session || sentRequestId !== requestId || r.status !== 200 || !r.data) return;
      sentAckVersion = r.data.version;
      expireWaitingResult(r.data);
    }, function () { /* the first poll started after the ack decides */ });
  }

  // Shows exactly what the hub said. Unknown is never retried.
  function settleSend(result) {
    audio.stopDictation();
    if (result.outcome === "handed_off") { sentRequestId = result.request_id; sentAckVersion = null; sentAckSeq = pollSeq; refreshAfterSend(result.request_id); }
    if (result.outcome === "handed_off" || result.outcome === "answered") $("draft").value = "";
    if (result.outcome === "unknown") {
      compose = freshCompose();
      compose.phase = "unknown";
      $("review").hidden = true;
      showResult(result.text, result.tone);
      render();
    } else {
      finishSend(result.text, result.tone);
    }
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

  function toggleDictation() {
    if (audio.dictating()) { audio.stopDictation(); return; }
    // Checked again at the tap: the render may predate the latest sound.
    if (audio.dictateBlocker()) { render(); return; }
    resetAfterResult();
    if (compose.phase === "review") { compose.phase = "edit"; $("review").hidden = true; }
    if (!freezeDestination()) { setNote("This target is not available for sends."); return; }
    audio.startDictation();
  }

  // ---- Replies ------------------------------------------------------------

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
        play.disabled = conv.holds();
        play.addEventListener("click", function () { audio.speak(rq.reply.spoken); });
        var row = el("div", { "class": "row" }, [play]);
        if (!audio.voiceAvailable()) row.appendChild(el("span", { "class": "note", text: "No speech voice on this device" }));
        li.appendChild(row);
      }
      list.appendChild(li);
    });
  }

  function describeState(rq) {
    return Logic.describeRequestState(rq.state) + " - " + formatTime(rq.created);
  }

  // ---- Conversation -------------------------------------------------------

  function startBlocker() {
    return window.DeskConversationLogic.startBlocker(state, selectedId, { recognizer: audio.speechInputAvailable(),
      voice: audio.voiceAvailable(), tapSending: tapSends > 0, canStart: conv.canStart(),
      blocker: conv.blocker() });
  }

  // A started conversation owns the target: the tap flow's review, ticket and dictation go.
  function startConversation() {
    batch(function () {
      if (!startBlocker() && conv.start(Logic.destinationOf(findTarget(selectedId), state.epoch))) voidCompose(null);
    });
  }

  // ---- Clear, enroll ------------------------------------------------------

  function doClear() {
    // Submit only the pair the operator confirmed, never the current lock.
    var frozen = clearFrozen;
    var t = frozen ? findTarget(frozen.session_id) : null;
    if (!frozen || !Logic.clearStillValid(frozen, t)) {
      clearFrozen = null;
      render();
      return;
    }
    api("POST", "/desk/api/clear", { session_id: frozen.session_id, lock_id: frozen.lock_id }).then(function (r) {
      clearFrozen = null;
      if (r.status === 200) hideResult();
      else showResult("Not cleared: " + errorText(r) + ".", "bad");
      render();
    }, function () {
      clearFrozen = null;
      showResult("Clear result unknown: the hub could not be reached.", "warn");
      render();
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
    conv = window.DeskConversation.create({ api: api, env: window.DeskConversation.browserEnv(window), deps: {
      selectedId: function () { return selectedId; }, visible: function () { return document.visibilityState === "visible"; },
      voiceAvailable: function () { return audio.voiceAvailable(); }, onChange: renderForController,
      lateSound: function () { return audio.lateSound(); } } });
    audio = window.DeskPageAudio.create({ win: window, $: $, conv: conv, note: setNote, changed: renderForController });
    convView = window.DeskConversationView.create(document, { start: startConversation, end: conv.endTap });
    startup = window.DeskStartup.create({
      api: api,
      labelFor: function (id) { var t = findTarget(id); return t ? t.label : null; }
    });
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
    $("clear-btn").addEventListener("click", function () {
      clearFrozen = Logic.freezeClear(selectedId === null ? null : findTarget(selectedId));
      render();
    });
    $("clear-no").addEventListener("click", function () { clearFrozen = null; render(); });
    $("clear-yes").addEventListener("click", doClear);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState !== "hidden") return;
      batch(function () {
        conv.hidden();
        audio.stopDictation();
        audio.stopSpeaking();
        if (compose.phase === "edit" || compose.phase === "review") {
          voidCompose("The page was hidden: the review and ticket were discarded.");
        }
      });
    });
    if ("speechSynthesis" in window) {
      window.speechSynthesis.addEventListener("voiceschanged", function () { if (state) render(); });
    }
    // Leaving is a silent end whatever visibilityState still says: a page
    // being unloaded or frozen can't be relied on to finish a cue.
    function leave() { batch(function () { conv.pageExit(); audio.stopSpeaking(); }); }
    document.addEventListener("freeze", leave);
    window.addEventListener("pagehide", leave);
  }

  wire();
  startPairing();
})();
