// The fetches a conversation has in flight, at most one of each kind, for
// the controller (conversation.js): each gets its own id when it begins, and
// its answer is used only while that id is still the current one of its
// kind, so the answer of an abandoned fetch is seen and dropped. After an
// end, two abandoned fetches may still be heard (`late`): the one whose
// status line ended the conversation, and the send POST the end left open.
// Each fetch keeps its own deadline, whatever becomes of the conversation.
// No I/O of its own, so node can load it; the controller's tests in
// tests/js/ drive it.
(function (root) {
  "use strict";

  // `nextId` is the controller's one id counter; `AbortCtl` the page's
  // AbortController, or null where there is none; `clock` its
  // {setTimeout, clearTimeout}.
  function create(nextId, AbortCtl, clock) {
    var current = {};        // kind -> the fetch in flight
    var live = {};           // id -> every fetch whose answer hasn't settled yet
    var late = { cause: null, send: null };

    // spec: {abort, ms, onDeadline}. `abort`: the conversation aborts it when
    // it gives up on it. `ms`: a hub call's own limit, kept whatever becomes
    // of the conversation (deadline below); the wake lock has none.
    function begin(kind, spec) {
      var f = { id: nextId(), ctl: AbortCtl ? new AbortCtl() : null, abortable: !!spec.abort, failed: false,
        expired: false, timer: null, onDeadline: spec.onDeadline || null };
      current[kind] = f;
      if (spec.ms) {
        f.timer = clock.setTimeout(function () { deadline(f); }, spec.ms);
        live[f.id] = f;
      }
      return f.id;
    }

    // Issues fetch `kind`: `call(opts)` makes its api call. Returns {id,
    // answer}, `answer` being that call's promise; the deadline is disarmed
    // once it settles, body read and all.
    function run(kind, spec, call) {
      var id = begin(kind, spec);
      var f = live[id];
      var answer = call(opts(kind));
      answer.then(function () { done(f); }, function () { done(f); });
      return { id: id, answer: answer };
    }

    function done(f) {
      delete live[f.id];
      if (f.timer !== null) clock.clearTimeout(f.timer);
      f.timer = null;
    }

    // A call's own limit: its body may settle what it would until then, and
    // at it the fetch is aborted, current, abandoned or left open by an end
    // alike, so no read outlives it. A grant request is the one exception
    // until its status line shows it failed: an abort can't prove the hub
    // won't still grant it (conversation.js, abandonGrant).
    function deadline(f) {
      f.timer = null;
      f.expired = true;
      if (f.onDeadline) f.onDeadline();
      if (f.ctl && (f.abortable || f.failed)) f.ctl.abort();
    }

    // Fetch `id` was answered with a status other than 200: the hub took no
    // action it could still take, so its read ends at its deadline, or now
    // when that has passed.
    function failed(id) {
      var f = live[id];
      if (!f) return;
      f.failed = true;
      if (f.expired && f.ctl) f.ctl.abort();
    }

    // The options the fetch's api call takes: its abort signal, if any, and
    // its id as `call`, which the page's api reports with each status line.
    function opts(kind) {
      var f = current[kind];
      if (!f) return {};
      return f.ctl ? { signal: f.ctl.signal, call: f.id } : { call: f.id };
    }

    function isCurrent(kind, id) { return !!current[kind] && current[kind].id === id; }

    // The kind of the current fetch with this id, or null.
    function kindOf(id) {
      var kinds = Object.keys(current).filter(function (kind) { return current[kind].id === id; });
      return kinds.length ? kinds[0] : null;
    }

    // True once for the current fetch of that kind; its answer is then used.
    function settle(kind, id) {
      if (!isCurrent(kind, id)) return false;
      delete current[kind];
      return true;
    }

    // Forgets the current fetch of a kind (its answer will be dropped), then
    // aborts it, unless its status line has already ended the conversation:
    // that one's body may still say why until its own deadline.
    function abandon(kind, abort) {
      var f = current[kind];
      if (!f) return false;
      delete current[kind];
      if (abort && f.ctl && f.id !== late.cause) f.ctl.abort();
      return true;
    }

    // Which late answer fetch `id` is, {cause, send}, true once for each role.
    function takeLate(id) {
      var role = { cause: id !== null && late.cause === id, send: id !== null && late.send === id };
      if (role.cause) late.cause = null;
      if (role.send) late.send = null;
      return role;
    }

    return {
      begin: begin, run: run, failed: failed, isCurrent: isCurrent, kindOf: kindOf, settle: settle,
      abandon: abandon, takeLate: takeLate,
      get: function (kind) { return current[kind] || null; },
      kinds: function () { return Object.keys(current); },
      // The fetch whose status line is ending the conversation; the send POST an end left open.
      lateCause: function (id) { late.cause = id; },
      lateSend: function (id) { late.send = id; },
      isLateSend: function (id) { return id !== null && id !== undefined && late.send === id; },
      // A new start: no earlier conversation's answer settles anything.
      forgetLate: function () { late.cause = null; late.send = null; }
    };
  }

  var api = { create: create };
  root.DeskConversationFetches = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
