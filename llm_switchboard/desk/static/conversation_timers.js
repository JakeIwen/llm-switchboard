// The conversation's timers, for the controller (conversation.js) and its
// fetches' deadlines (conversation_fetches.js): each is kept with the
// monotonic time it is due, so a limit is that time, not the moment its
// callback runs, which a busy event loop can delay past an answer that
// arrives after the limit. Every event from outside them (a hub answer, the
// wake lock's) enters through heard(), which applies it in one of two
// orders: one that silences the page first, with no overdue timer run
// before it, and any other only once every timer already due has run, in
// the order they fell due, exactly as if each had fired on time (catchUp).
// A timer's callback is told (`late` true) when it runs after something
// else that arrived past its time. No I/O of its own, so node can load it.
(function (root) {
  "use strict";

  // `env`: {now, setTimeout, clearTimeout}, `now` the page's monotonic ms
  // clock. Returns the same {setTimeout, clearTimeout}, plus heard(),
  // overdue() and lapse(); catchUp() is reached only through heard().
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

    // The one way an event from outside the timers is applied. `silences`:
    // the event is already known to silence the page (a status other than
    // 200, a lost body, an unusable snapshot, an answer that fails its
    // call's own check or came after that call's limit). It is applied
    // first and nothing overdue runs before it, since some due timers start
    // things (the guard opens the mic, the idle and turn limits speak a
    // closing cue) and the end it brings clears them all. Any other event
    // is applied after catchUp, so a limit already passed wins over a late
    // success. Returns what `apply()` returns.
    function heard(silences, apply) {
      if (!silences) catchUp();
      return apply();
    }

    // Whether timer `handle` is still to run and its time has come.
    function overdue(handle) {
      var t = due[handle];
      return !!t && t.at <= env.now();
    }

    // Runs those of `handles` already due, in the order they fell due, and
    // no other timer: a call's own limits, for an answer that came after them.
    function lapse(handles) {
      handles.filter(overdue).map(function (handle) { return due[handle]; }).sort(function (a, b) {
        return a.at - b.at || a.handle - b.handle;
      }).forEach(function (t) {
        env.clearTimeout(t.native);
        run(t.handle, true);
      });
    }

    return { setTimeout: setTimeout, clearTimeout: clearTimeout, heard: heard, overdue: overdue, lapse: lapse };
  }

  var api = { create: create };
  root.DeskConversationTimers = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
