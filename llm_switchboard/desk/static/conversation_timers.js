// The conversation's timers, for the controller (conversation.js) and its
// fetches' deadlines (conversation_fetches.js): each is kept with the
// monotonic time it is due, so a limit is that time, not the moment its
// callback runs, which a busy event loop can delay past an answer that
// arrives after the limit. catchUp(), before an answer is used, runs every
// timer already due, in the order they fell due, exactly as if each had
// fired on time; a timer's callback is told (`late` true) when it runs
// there, after something else that arrived past its time. No I/O of its
// own, so node can load it.
(function (root) {
  "use strict";

  // `env`: {now, setTimeout, clearTimeout}, `now` the page's monotonic ms
  // clock. Returns the same {setTimeout, clearTimeout}, plus catchUp().
  function create(env) {
    var seq = 0;
    var due = {}; // handle -> {at, handle, fn, native}, until it runs or is cleared

    function setTimeout(fn, ms) {
      var t = { at: env.now() + (ms || 0), handle: ++seq, fn: fn, native: null };
      t.native = env.setTimeout(function () { run(t.handle, false); }, ms);
      due[t.handle] = t;
      return t.handle;
    }

    function clearTimeout(handle) {
      var t = due[handle];
      if (!t) return;
      delete due[handle];
      env.clearTimeout(t.native);
    }

    function run(handle, late) {
      var t = due[handle];
      if (!t) return;
      delete due[handle];
      t.fn(late);
    }

    // The earliest timer due by `now`; of two due at once, the one set first,
    // the order the page's own timers keep.
    function next(now) {
      var first = null;
      Object.keys(due).forEach(function (handle) {
        var t = due[handle];
        if (t.at <= now && (!first || t.at < first.at || (t.at === first.at && t.handle < first.handle))) first = t;
      });
      return first;
    }

    // A timer a due one sets, already due itself, runs in the same pass.
    function catchUp() {
      for (var t = next(env.now()); t; t = next(env.now())) {
        env.clearTimeout(t.native);
        run(t.handle, true);
      }
    }

    return { setTimeout: setTimeout, clearTimeout: clearTimeout, catchUp: catchUp };
  }

  var api = { create: create };
  root.DeskConversationTimers = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
