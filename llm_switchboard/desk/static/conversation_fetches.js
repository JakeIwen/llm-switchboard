// The fetches a conversation has in flight, at most one of each kind, for
// the controller (conversation.js): each gets its own id when it begins, and
// its answer is used only while that id is still the current one of its
// kind, so the answer of an abandoned fetch is seen and dropped. No I/O of
// its own, so node can load it; the controller's tests in tests/js/ drive it.
(function (root) {
  "use strict";

  // `nextId` is the controller's one id counter; `AbortCtl` the page's
  // AbortController, or null where there is none.
  function create(nextId, AbortCtl) {
    var current = {};        // kind -> {id, ctl}

    function begin(kind, abortable) {
      var id = nextId();
      current[kind] = { id: id, ctl: abortable && AbortCtl ? new AbortCtl() : null };
      return id;
    }

    function signal(kind) {
      var f = current[kind];
      return f && f.ctl ? { signal: f.ctl.signal } : {};
    }

    function isCurrent(kind, id) { return !!current[kind] && current[kind].id === id; }

    // True once for the current fetch of that kind; its answer is then used.
    function settle(kind, id) {
      if (!isCurrent(kind, id)) return false;
      delete current[kind];
      return true;
    }

    // Forgets the current fetch of a kind (its answer will be dropped), then aborts it.
    function abandon(kind, abort) {
      var f = current[kind];
      if (!f) return false;
      delete current[kind];
      if (abort && f.ctl) f.ctl.abort();
      return true;
    }

    return {
      begin: begin, signal: signal, isCurrent: isCurrent, settle: settle, abandon: abandon,
      get: function (kind) { return current[kind] || null; },
      kinds: function () { return Object.keys(current); }
    };
  }

  var api = { create: create };
  root.DeskConversationFetches = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
