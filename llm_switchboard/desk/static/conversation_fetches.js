// The fetches a conversation has in flight, at most one of each kind, for
// the controller (conversation.js): each gets its own id when it begins, and
// its answer is used only while that id is still the current one of its
// kind, so the answer of an abandoned fetch is seen and dropped. After an
// end, two abandoned fetches may still be heard (`late`): the one whose
// status line ended the conversation, and the send POST the end left open.
// No I/O of its own, so node can load it; the controller's tests in
// tests/js/ drive it.
(function (root) {
  "use strict";

  // `nextId` is the controller's one id counter; `AbortCtl` the page's
  // AbortController, or null where there is none.
  function create(nextId, AbortCtl) {
    var current = {};        // kind -> {id, ctl}
    var late = { cause: null, send: null };

    function begin(kind, abortable) {
      var id = nextId();
      current[kind] = { id: id, ctl: abortable && AbortCtl ? new AbortCtl() : null };
      return id;
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
    // aborting that one could only lose the body that says why.
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
      begin: begin, opts: opts, isCurrent: isCurrent, kindOf: kindOf, settle: settle, abandon: abandon,
      takeLate: takeLate,
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
