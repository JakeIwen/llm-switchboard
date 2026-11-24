// The page's HTTP helper in the page harness (page_harness.js): what a
// status line establishes stands when the body is then lost. Run by
// tests/test_desk_conversation_js.py with `node --test`.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { L, PATH, REASON, settle, paired, converse, ended, voiceSend } = require("./page_harness.js");

async function turn() {
  const h = await paired();
  await converse(h);
  h.rec().hear("check the logs");
  h.rec().hear("send it");
  await settle();
  return h;
}

test("a voice send's 401 drops the pairing at its headers, and a lost body changes nothing", async () => {
  const h = await paired();
  await converse(h);
  await voiceSend(h, "check the logs");
  const said = h.said.length;
  const body = await h.headers(PATH.SEND, 401);
  assert.equal(h.$("pairing").hidden, false);
  assert.equal(h.said.length, said);
  await body.lose();
  assert.equal(h.$("pairing").hidden, false);
  assert.equal(h.said.length, said, "the page spoke after the hub refused its token");
  assert.equal(h.state(), ended(REASON.UNPAIRED + "; " + L.withDetail(REASON.NOT_SENT, "HTTP 401")));
});

test("a ticket's or send's refusal whose body is lost is an answer: silent, its status shown, never 'unreachable'", async () => {
  const t = await turn();
  const said = t.said.length;
  await (await t.headers(PATH.TICKET, 409)).lose();
  assert.equal(t.state(), ended(L.withDetail(REASON.CALL_FAILED, "HTTP 409")));
  assert.equal(t.said.length, said);
  const s = await turn();
  await s.answer(PATH.TICKET, 200, { ticket: "t1" });
  const before = s.said.length;
  await (await s.headers(PATH.SEND, 409)).lose();
  assert.equal(s.state(), ended(L.withDetail(REASON.CALL_FAILED, "HTTP 409") + "; " + REASON.UNKNOWN));
  assert.equal(s.said.length, before);
});

test("the tap flow: a 401 drops the pairing at its headers, and a lost body is still the hub's answer", async () => {
  const h = await paired();
  h.$("draft").value = "hello";
  h.$("draft").dispatch("input");
  await (await h.headers("/desk/api/ticket", 409)).lose();
  assert.equal(h.$("compose-note").textContent, "No ticket: HTTP 409");
  h.$("draft").dispatch("input");
  await h.answer("/desk/api/ticket", 200, { ticket: "tap-ticket" });
  h.$("review-btn").click();
  h.$("send-btn").click();
  await settle();
  await h.headers("/desk/api/send", 401);
  assert.equal(h.$("pairing").hidden, false);
});

test("a startup acknowledgement's 5xx whose body is lost reads as unknown, not refused", async () => {
  const h = await paired();
  await h.answer("/desk/api/startup", 200, { sessions: [{ session_id: "s1", digest: "d1", capture: "warning",
    refusal: null }] });
  const ack = h.$("startup-list").all((k) => k.getAttribute("data-testid") === "startup-ack")[0];
  ack.click();
  await settle();
  await (await h.headers("/desk/api/startup/ack", 503)).lose();
  const outcome = h.$("startup-outcomes").textContent;
  assert.match(outcome, /^Result unknown for .*: the hub's answer \(HTTP 503\) could not be read\./);
});
