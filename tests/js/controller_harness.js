// The conversation controller wired to fake recognition, speech, timers,
// wake lock and fetch, shared by the controller's Node tests and the probe
// tests/test_desk_conversation_js.py drives with the hub's own refusals.
"use strict";
const assert = require("node:assert/strict");
const L = require("../../llm_switchboard/desk/static/conversation_logic.js");
const C = require("../../llm_switchboard/desk/static/conversation.js");
const { PHASE, CUE } = L;
const { PATH } = C;

const DEST = { host: "pi", session_id: "s1", generation: 3, label: "one", epoch: "e1" };
const flush = () => new Promise((resolve) => setImmediate(resolve));

function target(over) {
  return Object.assign({ session_id: "s1", generation: 3, registration: "replied", enrolled: true, lock: null }, over);
}
let version = 100;
function snap(over) {
  return Object.assign({
    version: ++version, epoch: "e1", sends_enabled: true, voice_confirm_enabled: true, lock_store_error: null,
    targets: [target()], requests: []
  }, over);
}
function rq(id, state, spoken) {
  return { request_id: id, session_id: "s1", state: state, reply: spoken === undefined ? null : { text: "t", spoken: spoken } };
}

function harness() {
  const h = {
    t: 1000, timers: [], handle: 0, recs: [], said: [], calls: [], selected: "s1", visible: true, voice: true,
    autoAudio: true, honorAbort: true, recThrows: false, sentinels: [], wakeFails: false, changes: []
  };
  class FakeRecognition {
    constructor() { this.starts = 0; this.aborts = 0; h.recs.push(this); }
    start() {
      if (h.recThrows) throw new Error("not allowed");
      this.starts++;
      if (h.autoAudio) queueMicrotask(() => this.fire("audiostart"));
    }
    abort() { this.aborts++; }
    fire(name, ev) { if (this["on" + name]) this["on" + name](ev || {}); }
    result(text, final) {
      const result = [{ transcript: text }];
      result.isFinal = final !== false;
      this.fire("result", { resultIndex: 0, results: [result] });
    }
  }
  class FakeUtterance {
    constructor(text) { this.text = text; this.listeners = {}; }
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
    fire(name, ev) {
      if (name === "end" || name === "error") h.synth.speaking = false;
      if (this["on" + name]) this["on" + name](ev || {});
      (this.listeners[name] || []).forEach((fn) => fn(ev || {}));
    }
  }
  h.synth = {
    speaking: false, pending: false, cancels: 0,
    speak(u) { h.said.push(u); this.speaking = true; },
    cancel() { this.cancels++; this.speaking = false; }
  };
  function api(method, path, body, opts) {
    return new Promise((resolve, reject) => {
      const call = { method, path, body, done: false };
      call.answer = (status, data) => { call.done = true; resolve({ status, data }); };
      call.fail = () => { call.done = true; reject(new Error("network")); };
      if (opts && opts.signal && h.honorAbort) {
        opts.signal.addEventListener("abort", () => { call.aborted = true; call.done = true; reject(new Error("abort")); });
      }
      h.calls.push(call);
    });
  }
  const env = {
    Recognition: FakeRecognition, Utterance: FakeUtterance, synth: h.synth, AbortController: AbortController,
    now: () => h.t,
    setTimeout: (fn, ms) => { const id = ++h.handle; h.timers.push({ id, at: h.t + ms, fn }); return id; },
    clearTimeout: (id) => { h.timers = h.timers.filter((x) => x.id !== id); },
    wakeLock: {
      request: () => {
        if (h.wakeFails) return Promise.reject(new Error("denied"));
        const s = { released: 0, listeners: [] };
        s.addEventListener = (name, fn) => s.listeners.push(fn);
        s.release = () => { s.released++; return Promise.resolve(); };
        h.sentinels.push(s);
        return Promise.resolve(s);
      }
    }
  };
  h.Utterance = FakeUtterance;
  h.ctl = C.create({
    api, env,
    deps: { selectedId: () => h.selected, visible: () => h.visible, voiceAvailable: () => h.voice, onChange: (v) => { h.changes.push(v); } }
  });
  h.view = () => h.ctl.view();
  h.phase = () => h.ctl.view().phase;
  h.rec = () => h.recs[h.recs.length - 1];
  h.utt = () => h.said[h.said.length - 1];
  h.pending = (path) => h.calls.filter((x) => x.path === path && !x.done);
  h.all = (path) => h.calls.filter((x) => x.path === path);
  h.answer = async (path, status, data) => {
    const call = h.pending(path)[0];
    assert.ok(call, "no pending call to " + path);
    call.answer(status, data);
    await flush();
    return call;
  };
  // Runs every timer due within `ms`, in order, settling promises between them.
  h.advance = async (ms) => {
    const end = h.t + ms;
    for (;;) {
      const due = h.timers.filter((x) => x.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      h.timers = h.timers.filter((x) => x !== due);
      h.t = due.at;
      due.fn();
      await flush();
    }
    h.t = end;
    await flush();
  };
  // Time passing with the hub answering every beat.
  h.alive = async (ms) => {
    for (let left = ms; left > 0; left -= 500) {
      await h.advance(Math.min(500, left));
      for (const call of h.pending(PATH.BEAT)) { call.answer(200, { grant: "g1", expires_in: 15 }); await flush(); }
    }
  };
  // The current utterance plays to its end.
  h.finish = async () => { h.utt().fire("end"); await flush(); };
  h.hear = async (text) => { h.rec().result(text); await flush(); };
  return h;
}

async function listening(h, s) {
  assert.equal(h.ctl.start(DEST), true);
  await flush();
  await h.answer(PATH.STATE, 200, s || snap());
  await h.answer(PATH.GRANT, 200, { grant: "g1", expires_in: 15 });
  assert.equal(h.phase(), PHASE.LISTENING);
  assert.equal(h.rec().starts, 1);
  return h;
}

// From listening: draft, "send it", ticket, POST answered with request `id`.
async function sent(h, id, text) {
  await h.hear(text || "check the logs");
  await h.hear("send it");
  assert.equal(h.phase(), PHASE.SENDING);
  const ticket = await h.answer(PATH.TICKET, 200, { ticket: "t-" + id });
  assert.deepEqual(ticket.body, { session_id: "s1", generation: 3, epoch: "e1" });
  const post = await h.answer(PATH.SEND, 200, { request_id: id, state: "handed_off" });
  assert.deepEqual(post.body, { ticket: "t-" + id, text: text || "check the logs", confirm: "voice", grant: "g1" });
  assert.equal(h.phase(), PHASE.WAITING);
  assert.equal(h.utt().text, CUE.SENT);
}

function texts(h) { return h.said.map((u) => u.text); }
function releases(h) { return h.all(PATH.RELEASE).map((x) => x.body.grant); }

module.exports = { DEST, flush, target, snap, rq, harness, listening, sent, texts, releases };
