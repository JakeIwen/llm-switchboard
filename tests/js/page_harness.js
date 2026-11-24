// The tablet page in one window for the page's Node tests: the real index.html
// ids, the real scripts index.html loads, in its order, with a small fake DOM,
// fetch, timers, recognition, speech and wake lock, and the steps the tests
// share (pairing, a Start through to the open mic, a voice send).
"use strict";
const assert = require("node:assert/strict");
const { G } = require("./grant_ids.js");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "llm_switchboard", "desk", "static");
const L = require(path.join(STATIC, "conversation_logic.js"));
const { PATH } = require(path.join(STATIC, "conversation.js"));
const { REASON, BLOCKER, CUE, LIMITS } = L;
const HTML = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const SCRIPTS = [...HTML.matchAll(/<script src="\/desk\/([^"]+)"><\/script>/g)].map((m) => m[1]);
const POLL = PATH.STATE + "?after=";

const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

// Just enough DOM for app.js: elements by id from index.html, children,
// text, attributes and listeners.
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.listeners = {};
    this.hidden = false; this.disabled = false; this.inert = false; this.value = ""; this.className = ""; this.own = "";
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(kid) { this.children.push(kid); return kid; }
  removeChild(kid) { this.children.splice(this.children.indexOf(kid), 1); return kid; }
  set textContent(v) { this.own = String(v); this.children = []; }
  get textContent() { return this.own + this.children.map((k) => k.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  dispatch(name) { (this.listeners[name] || []).forEach((fn) => fn({ preventDefault() {} })); }
  click() { if (!this.disabled) this.dispatch("click"); }
  all(pred) { return this.children.flatMap((k) => (pred(k) ? [k] : []).concat(k.all(pred))); }
}

function page(opts) {
  opts = opts || {};
  // honorAbort false: an abort the page issues is lost, as when the answer
  // was already on its way, and the call stays open for the test to answer.
  const h = { t: 0, handle: 0, timers: [], calls: [], said: [], recs: [], sentinels: [], honorAbort: true };
  const byId = {};
  for (const m of HTML.matchAll(/<(\w+)\s([^>]*?)\bid="([^"]+)"([^>]*)>/g)) {
    const el = new El(m[1]);
    el.hidden = /(^|\s)hidden(\s|$|>)/.test(m[2] + " " + m[4]);
    byId[m[3]] = el;
  }
  const docListeners = {};
  const document = {
    visibilityState: "visible", activeElement: null,
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => new El(tag),
    querySelector: () => ({ getAttribute: () => "build-1" }),
    addEventListener: (name, fn) => { (docListeners[name] = docListeners[name] || []).push(fn); }
  };
  // Like a browser, a disabled control can't take focus.
  for (const el of Object.values(byId)) el.focus = () => { if (!el.disabled) document.activeElement = el; };
  class Utterance {
    constructor(text) { this.text = text; this.listeners = {}; }
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
    fire(name, ev) {
      if (this.over) return;
      this.over = true;
      synth.speaking = false;
      if (this["on" + name]) this["on" + name](ev || {});
      (this.listeners[name] || []).forEach((fn) => fn(ev || {}));
    }
  }
  // Like Chrome: cancel() makes every unfinished utterance report an error, later.
  const synth = {
    speaking: false, pending: false, cancels: 0, voices: [{ name: "v" }],
    getVoices() { return this.voices; },
    addEventListener() {},
    speak(u) { h.said.push(u); this.speaking = true; },
    cancel() {
      this.cancels++;
      this.speaking = false;
      h.said.filter((u) => !u.over).forEach((u) => Promise.resolve().then(() => u.fire("error", { error: "interrupted" })));
    }
  };
  class Recognition {
    constructor() { this.starts = 0; this.aborts = 0; h.recs.push(this); }
    start() { this.starts++; queueMicrotask(() => this.onaudiostart && this.onaudiostart({})); }
    abort() { this.aborts++; }
    hear(text) {
      const result = [{ transcript: text }];
      result.isFinal = true;
      this.onresult({ resultIndex: 0, results: [result] });
    }
  }
  function fetch(url, init) {
    return new Promise((resolve, reject) => {
      const call = { path: url, method: init.method, body: init.body ? JSON.parse(init.body) : undefined,
        auth: init.headers.Authorization || null, done: false };
      call.answer = (status, data) => {
        call.done = true;
        resolve({ status, text: () => Promise.resolve(data === undefined ? "" : JSON.stringify(data)) });
      };
      // The status line arrives; the body read waits until `arrive(data)`
      // delivers the body (`raw(text)` delivers text as it is), or `lose()`
      // makes it fail, as on a connection cut mid-response, or an abort
      // does, as in a browser.
      call.headers = (status) => {
        call.done = true;
        let lose, deliver;
        const body = new Promise((res, rej) => {
          lose = () => rej(new Error("connection reset"));
          deliver = res;
        });
        call.loseBody = lose;
        resolve({ status, text: () => body });
        const then = async (step) => { step(); await settle(); };
        return {
          lose: () => then(lose),
          arrive: (data) => then(() => deliver(data === undefined ? "" : JSON.stringify(data))),
          raw: (text) => then(() => deliver(text))
        };
      };
      if (init.signal) init.signal.addEventListener("abort", () => {
        if (!h.honorAbort) return;
        call.aborted = true;
        if (!call.done) { call.done = true; reject(new Error("abort")); } else if (call.loseBody) call.loseBody();
      });
      h.calls.push(call);
      if (url === "/desk/pair/start") call.answer(200, { pair_id: "p1", code: "ABC-DEF", expires_in: 300 });
      if (url === "/desk/pair/claim") call.answer(200, { token: "tok" });
    });
  }
  const win = {
    document, fetch, TextEncoder, AbortController, speechSynthesis: synth, SpeechSynthesisUtterance: Utterance,
    crypto: { getRandomValues: (a) => a, subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) } },
    performance: { now: () => h.t },
    navigator: { wakeLock: { request: () => {
      const s = { released: 0, addEventListener() {}, release() { this.released++; return Promise.resolve(); } };
      h.sentinels.push(s);
      return Promise.resolve(s);
    } } },
    setTimeout: (fn, ms) => { const id = ++h.handle; h.timers.push({ id, at: h.t + (ms || 0), fn }); return id; },
    clearTimeout: (id) => { h.timers = h.timers.filter((x) => x.id !== id); },
    addEventListener: (name, fn) => { (docListeners["window:" + name] = docListeners["window:" + name] || []).push(fn); }
  };
  if (!opts.noRecognizer) win.webkitSpeechRecognition = Recognition;
  win.window = win;
  vm.createContext(win);
  for (const name of SCRIPTS) vm.runInContext(fs.readFileSync(path.join(STATIC, name), "utf8"), win, { filename: name });

  Object.assign(h, { win, document, synth, $: (id) => byId[id] });
  h.pending = (p) => h.calls.filter((c) => !c.done && (p.endsWith("=") ? c.path.startsWith(p) : c.path === p));
  h.all = (p) => h.calls.filter((c) => c.path === p);
  h.answer = async (p, status, data) => {
    const call = h.pending(p)[0];
    assert.ok(call, "no pending call to " + p);
    call.answer(status, data);
    await settle();
    return call;
  };
  // `p` is a path, or the call itself when several are open on one path.
  h.headers = async (p, status) => {
    const call = typeof p === "string" ? h.pending(p)[0] : p;
    assert.ok(call && !call.done, "no pending call to " + (call ? call.path : p));
    const body = call.headers(status);
    await settle();
    return body;
  };
  h.advance = async (ms) => {
    const end = h.t + ms;
    for (;;) {
      const due = h.timers.filter((x) => x.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!due) break;
      h.timers = h.timers.filter((x) => x !== due);
      h.t = due.at;
      due.fn();
      await settle();
    }
    h.t = end;
    await settle();
  };
  h.fire = async (name) => { (docListeners[name] || []).forEach((fn) => fn({})); await settle(); };
  h.state = () => h.$("conv-state").textContent;
  h.utt = () => h.said[h.said.length - 1];
  h.rec = () => h.recs[h.recs.length - 1];
  h.target = (id) => h.$("targets").all((k) => k.getAttribute("data-session") === id)[0];
  h.plays = () => h.$("replies").all((k) => k.getAttribute("data-testid") === "play");
  return h;
}

let version = 0;
function snap(over) {
  return Object.assign({
    version: ++version, epoch: "e1", sends_enabled: true, voice_confirm_enabled: true, lock_store_error: null,
    targets: [target("s1")], requests: []
  }, over);
}
function target(id, over) {
  return Object.assign({ host: "pi", session_id: id, label: "label " + id, cwd: "/w", enrolled: true,
    registration: "replied", generation: 3, lock: null }, over);
}
function rq(id, state, spoken, confirm) {
  return { request_id: id, session_id: "s1", state, text: "asked", created: 1, confirm: confirm || "tap",
    reply: spoken === undefined ? null : { text: "full", spoken } };
}
const voiceRq = (id, state, spoken) => rq(id, state, spoken, L.CONFIRM.VOICE);

// Pairs the page and delivers its first snapshot.
async function paired(first, opts) {
  const h = page(opts);
  await settle();
  await h.advance(2000);
  assert.ok(h.$("pairing").hidden, "the page did not pair");
  await h.answer(PATH.STATE, 200, first || snap());
  return h;
}

// A tap on Start conversation, through to the open mic.
async function converse(h, s) {
  h.$("conv-start").click();
  await settle();
  assert.equal(h.$("conv-overlay").hidden, false);
  await h.answer(PATH.STATE, 200, s || snap());
  await h.answer(PATH.GRANT, 200, { grant: G.g1, expires_in: 15 });
  assert.equal(h.state(), "Listening.");
}

const ended = (reason) => CUE.ENDED + ": " + reason + ".";
const releases = (h) => h.all(PATH.RELEASE).map((c) => c.body.grant);

// The Play control of request `id`.
const playFor = (h, id) => h.$("replies").all((k) => k.getAttribute("data-request") === id)[0]
  .all((k) => k.getAttribute("data-testid") === "play")[0];

// Like Chrome under load: cancel() empties the queue, but the cancelled
// utterances' errors come later, when the test fires them.
function slowCancel(h) {
  h.synth.cancel = function () { this.cancels++; this.speaking = false; };
}

// Through "send it" to the voice send's POST, left pending.
async function voiceSend(h, words) {
  h.rec().hear(words);
  h.rec().hear("send it");
  await settle();
  await h.answer(PATH.TICKET, 200, { ticket: "t1" });
  assert.equal(h.pending(PATH.SEND).length, 1);
}

const last = (h, p) => h.pending(p)[h.pending(p).length - 1];

// A Start tap through to the open mic, answering the newest Start snapshot
// and grant request, so older calls on the same paths stay open.
async function listen(h, grant, s) {
  if (h.$("conv-end").textContent === "Close") h.$("conv-end").click();
  h.$("conv-start").click();
  await settle();
  last(h, PATH.STATE).answer(200, s || snap());
  await settle();
  last(h, PATH.GRANT).answer(200, { grant: grant || G.g1, expires_in: 15 });
  await settle();
  assert.equal(h.state(), "Listening.");
}

// End tapped: the closing cue plays.
async function cue(h) {
  h.$("conv-end").click();
  await settle();
  assert.match(h.utt().text, /^Conversation ended: End was tapped/);
  assert.equal(h.synth.speaking, true);
}

// A conversation ended by its closing cue's own end, the guard passed.
async function over(h) {
  await cue(h);
  h.utt().fire("end");
  await settle();
  await h.advance(LIMITS.GUARD_MS);
}

async function drafted(h) {
  h.rec().hear("check the logs");
  h.rec().hear("send it");
  await settle();
}

// A Start snapshot given up on at the step deadline, its abort lost.
async function abandonedSnapshot(h) {
  h.honorAbort = false;
  h.$("conv-start").click();
  await settle();
  const s = h.pending(PATH.STATE)[0];
  await h.advance(LIMITS.STEP_MS);
  assert.equal(h.state(), ended(REASON.HUB_UNREACHABLE));
  h.honorAbort = true;
  return s;
}

// A grant request given up on at the step deadline, let go when a poll
// showed a new hub epoch.
async function abandonedGrant(h) {
  h.$("conv-start").click();
  await settle();
  await h.answer(PATH.STATE, 200, snap());
  const g = h.pending(PATH.GRANT)[0];
  await h.advance(LIMITS.STEP_MS);
  await h.answer(POLL, 200, snap({ epoch: "e2" }));
  return g;
}

function tapTicket(h) {
  h.$("draft").value = "hello";
  h.$("draft").dispatch("input");
}

// A tapped send handed off: its state refresh is left open.
async function tapRefresh(h) {
  tapTicket(h);
  await h.answer("/desk/api/ticket", 200, { ticket: "tap-ticket" });
  h.$("review-btn").click();
  h.$("send-btn").click();
  await settle();
  await h.answer("/desk/api/send", 200, { request_id: "tap1", state: "handed_off" });
  return h.pending(PATH.STATE)[0];
}

module.exports = {
  STATIC, L, PATH, REASON, BLOCKER, CUE, POLL, settle, page, snap,
  target, rq, voiceRq, paired, converse, ended, releases, playFor, slowCancel, voiceSend,
  last, listen, cue, over, drafted, abandonedSnapshot, abandonedGrant, tapTicket, tapRefresh
};
