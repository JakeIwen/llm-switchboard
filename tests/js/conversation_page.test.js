// The tablet page's conversation wiring: the real index.html ids, the real
// scripts index.html loads, in its order, in one window, with a small
// fake DOM, fetch, timers, recognition, speech and wake lock. Asserts through
// what the page shows and what it sends. Run by
// tests/test_desk_conversation_js.py with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC = path.join(__dirname, "..", "..", "llm_switchboard", "desk", "static");
const L = require(path.join(STATIC, "conversation_logic.js"));
const { PATH } = require(path.join(STATIC, "conversation.js"));
const { REASON, BLOCKER, CUE } = L;
const HTML = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const SCRIPTS = [...HTML.matchAll(/<script src="\/desk\/([^"]+)"><\/script>/g)].map((m) => m[1]);
const POLL = PATH.STATE + "?after=";

const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };

// Just enough DOM for app.js: elements by id from index.html, children,
// text, attributes and listeners.
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.listeners = {};
    this.hidden = false; this.disabled = false; this.value = ""; this.className = ""; this.own = "";
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
  const h = { t: 0, handle: 0, timers: [], calls: [], said: [], recs: [], sentinels: [] };
  const byId = {};
  for (const m of HTML.matchAll(/<(\w+)\s([^>]*?)\bid="([^"]+)"([^>]*)>/g)) {
    const el = new El(m[1]);
    el.hidden = /(^|\s)hidden(\s|$|>)/.test(m[2] + " " + m[4]);
    byId[m[3]] = el;
  }
  const docListeners = {};
  const document = {
    visibilityState: "visible",
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => new El(tag),
    querySelector: () => ({ getAttribute: () => "build-1" }),
    addEventListener: (name, fn) => { (docListeners[name] = docListeners[name] || []).push(fn); }
  };
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
      if (init.signal) init.signal.addEventListener("abort", () => { if (!call.done) { call.done = true; reject(new Error("abort")); } });
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
function rq(id, state, spoken) {
  return { request_id: id, session_id: "s1", state, text: "asked", created: 1,
    reply: spoken === undefined ? null : { text: "full", spoken } };
}

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
  await h.answer(PATH.GRANT, 200, { grant: "g1", expires_in: 15 });
  assert.equal(h.state(), "Listening.");
}

const ended = (reason) => CUE.ENDED + ": " + reason + ".";
const releases = (h) => h.all(PATH.RELEASE).map((c) => c.body.grant);

test("a Start tap voids an open review and its ticket", async () => {
  const h = await paired();
  h.$("draft").value = "hello";
  h.$("draft").dispatch("input");
  await h.answer("/desk/api/ticket", 200, { ticket: "tap-ticket" });
  h.$("review-btn").click();
  assert.equal(h.$("review").hidden, false);
  await converse(h);
  assert.equal(h.$("review").hidden, true);
  // The conversation's own ticket is requested later; the tap flow's is gone.
  h.$("send-btn").click();
  await settle();
  assert.equal(h.all("/desk/api/send").length, 0);
});

test("a Start tap stops one-shot dictation", async () => {
  const h = await paired();
  h.$("dictate").click();
  await h.answer("/desk/api/ticket", 200, { ticket: "tap-ticket" });
  const dictation = h.rec();
  assert.equal(dictation.starts, 1);
  await converse(h);
  assert.equal(dictation.aborts, 1);
  assert.equal(h.$("dictate").textContent, "Dictate");
  assert.notEqual(h.rec(), dictation, "the conversation opens its own recognizer");
});

test("selecting another target ends the conversation, and its closing cue is not cut", async () => {
  const two = () => snap({ targets: [target("s1"), target("s2")] });
  const h = await paired(two());
  h.target("s1").click();
  await converse(h, two());
  const cancels = h.synth.cancels;
  h.target("s2").click();
  await settle();
  assert.equal(h.state(), ended(REASON.RETARGET));
  assert.equal(h.utt().text, ended(REASON.RETARGET));
  assert.equal(h.synth.cancels, cancels, "the page cancelled the controller's closing cue");
  assert.deepEqual(releases(h), []);
  h.utt().fire("end");
  await settle();
  assert.deepEqual(releases(h), ["g1"]);
});

test("hiding the page ends the conversation without a cue", async () => {
  const h = await paired();
  await converse(h);
  const said = h.said.length;
  h.document.visibilityState = "hidden";
  await h.fire("visibilitychange");
  assert.equal(h.state(), ended(REASON.HIDDEN));
  assert.equal(h.said.length, said);
  assert.deepEqual(releases(h), ["g1"]);
  assert.equal(h.rec().aborts, 1);
});

test("the page being frozen or unloaded ends the conversation", async () => {
  for (const event of ["freeze", "window:pagehide"]) {
    const h = await paired();
    await converse(h);
    await h.fire(event);
    assert.equal(h.state(), ended(REASON.HIDDEN), event);
  }
});

test("a dropped token ends the conversation and speaks nothing", async () => {
  const h = await paired();
  await converse(h);
  const said = h.said.length;
  await h.answer(POLL, 401, { error: "unauthorized" });
  assert.equal(h.state(), ended(REASON.UNPAIRED));
  assert.equal(h.said.length, said);
  assert.equal(h.$("pairing").hidden, false);
  assert.deepEqual(releases(h), ["g1"]);
});

test("the tap flow's controls are disabled while a conversation holds the voice, and return after", async () => {
  const s = () => snap({ requests: [rq("old", "answered", "Earlier reply.")] });
  const h = await paired(s());
  // Send lives in the review panel, which a Start closes and Review can't reopen.
  const controls = () => [h.$("dictate"), h.$("review-btn"), h.$("test-btn"), h.$("draft"), ...h.plays()];
  assert.equal(h.plays().length, 1);
  assert.ok(controls().slice(0, 4).every((c) => !c.disabled));
  await converse(h, s());
  assert.ok(controls().every((c) => c.disabled));
  assert.equal(h.$("review").hidden, true);
  h.$("conv-end").click();
  await settle();
  assert.equal(h.state(), ended(REASON.END_TAPPED));
  assert.equal(h.$("conv-end").textContent, "Close");
  // The closing cue still holds the voice.
  assert.ok(controls().every((c) => c.disabled));
  h.utt().fire("end");
  await settle();
  assert.ok(controls().slice(0, 4).every((c) => !c.disabled));
  assert.ok(h.plays().every((c) => !c.disabled));
  h.$("conv-end").click();
  assert.equal(h.$("conv-overlay").hidden, true);
});

test("a reply that arrives during a conversation is spoken once, by the controller", async () => {
  const h = await paired();
  await converse(h);
  h.rec().hear("check the logs");
  h.rec().hear("send it");
  await settle();
  await h.answer(PATH.TICKET, 200, { ticket: "t1" });
  const post = await h.answer(PATH.SEND, 200, { request_id: "r1", state: "handed_off" });
  assert.equal(post.body.confirm, "voice");
  assert.equal(h.utt().text, CUE.SENT);
  h.utt().fire("end");
  await settle();
  const lock = { lock_id: "L1", request_id: "r1", since: 1, earlier_run: false };
  await h.answer(POLL, 200, snap({ targets: [target("s1", { lock })], requests: [rq("r1", "handed_off")] }));
  await h.answer(POLL, 200, snap({ requests: [rq("r1", "answered", "All good.")] }));
  assert.equal(h.utt().text, "All good.");
  assert.equal(h.state(), "Reading the reply. The mic is closed.");
  h.$("conv-end").click();
  await settle();
  h.utt().fire("end");
  await settle();
  await h.answer(POLL, 200, snap({ requests: [rq("r1", "answered", "All good.")] }));
  assert.equal(h.said.filter((u) => u.text === "All good.").length, 1);
});

test("Start is disabled with its reason", async () => {
  const cases = [
    [snap({ voice_confirm_enabled: false }), BLOCKER.VOICE_OFF],
    [snap({ sends_enabled: false }), BLOCKER.SENDS_OFF],
    [snap({ requests: [rq("r9", "handed_off")] }), BLOCKER.BUSY],
    [snap({ targets: [target("s1", { lock: { lock_id: "L", request_id: "r9", since: 1, earlier_run: true } })] }), BLOCKER.BUSY]
  ];
  for (const [first, reason] of cases) {
    const h = await paired(first);
    assert.equal(h.$("conv-start").disabled, true, reason);
    assert.equal(h.$("conv-why").hidden, false);
    assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + reason + ".");
    h.$("conv-start").click();
    await settle();
    assert.equal(h.all(PATH.GRANT).length + h.pending(PATH.STATE).length, 0);
  }
  const ok = await paired();
  assert.equal(ok.$("conv-start").disabled, false);
  assert.equal(ok.$("conv-why").hidden, true);
});

test("Start is disabled without a recognizer", async () => {
  const h = await paired(snap(), { noRecognizer: true });
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + BLOCKER.NO_RECOGNIZER + ".");
});

test("Start stays disabled while sound is uncertain after a cut-off utterance", async () => {
  const h = await paired();
  await converse(h);
  h.rec().hear("open the hatch");
  h.rec().hear("read it back");
  await settle();
  assert.equal(h.utt().text, "open the hatch");
  h.$("conv-end").click();
  await settle();
  // Cut off mid read-back: no closing cue, and Start waits out twice the estimate.
  assert.equal(h.utt().text, "open the hatch");
  h.$("conv-end").click();
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + BLOCKER.NOT_QUIET + ".");
  await h.advance(L.estimateMs("open the hatch") * L.SPEECH_DEADLINE_FACTOR);
  assert.equal(h.$("conv-start").disabled, false);
});

test("the page's own Play utterance is tracked toward Start's quiet rule", async () => {
  const h = await paired(snap({ requests: [rq("old", "answered", "Earlier reply.")] }));
  h.plays()[0].click();
  await settle();
  assert.equal(h.utt().text, "Earlier reply.");
  assert.equal(h.$("conv-start").disabled, true);
  assert.equal(h.$("conv-why").textContent, "Start conversation is unavailable: " + BLOCKER.NOT_QUIET + ".");
  h.utt().fire("end");
  await settle();
  assert.equal(h.$("conv-start").disabled, false);
});
