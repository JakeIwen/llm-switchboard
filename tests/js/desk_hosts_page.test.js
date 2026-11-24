// The tablet page's targets as (host, session_id) (docs/voice-desk.md,
// "Enrollment for Mac sessions", "The page"), through the real page:
// selection, history, speech and the tap flow's ticket.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { POLL, paired, snap, target, rq, settle, tapTicket } = require("./page_harness.js");

const onMac = (over) => target("s1", Object.assign({ host: "mac", generation: 9 }, over));
const macRq = (id, state, spoken) => Object.assign(rq(id, state, spoken), { host: "mac" });
const shown = (h) => h.$("replies").all((k) => k.getAttribute("data-testid") === "request")
  .map((k) => k.getAttribute("data-request"));

test("a session id that moves from the Pi to a Mac drops the selection, and the Mac target shows only its own requests", async () => {
  const h = await paired(snap({ requests: [rq("r1", "answered", "pi words")] }));
  assert.equal(h.$("detail").hidden, false);
  assert.deepEqual(shown(h), ["r1"]);
  await h.answer(POLL, 200, snap({ targets: [onMac(), target("s2")],
    requests: [rq("r1", "answered", "pi words"), macRq("r2", "answered", "mac words")] }));
  assert.equal(h.$("detail").hidden, true, "the Pi target's selection survived the move");
  h.target("s1").click();
  await settle();
  assert.equal(h.target("s1").getAttribute("data-host"), "mac");
  assert.deepEqual(shown(h), ["r2"]);
  assert.ok(!h.$("replies").textContent.includes("pi words"));
});

test("a reply is spoken only for the selected target's host", async () => {
  const h = await paired(snap({ targets: [onMac()] }));
  const said = h.said.length;
  await h.answer(POLL, 200, snap({ targets: [onMac()], requests: [rq("r1", "answered", "pi words")] }));
  assert.equal(h.said.length, said, "a Pi reply was spoken under the Mac target");
  await h.answer(POLL, 200, snap({ targets: [onMac()],
    requests: [rq("r1", "answered", "pi words"), macRq("r2", "answered", "mac words")] }));
  assert.equal(h.said.length, said + 1);
  assert.equal(h.said[h.said.length - 1].text, "mac words");
});

test("the tap flow's ticket names the target's host", async () => {
  const h = await paired(snap({ targets: [onMac()] }));
  tapTicket(h);
  await settle();
  const [call] = h.pending("/desk/api/ticket");
  assert.deepEqual(call.body, { session_id: "s1", generation: 9, epoch: "e1", host: "mac" });
});
