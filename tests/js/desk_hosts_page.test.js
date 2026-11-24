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

// ---- Host badges, link and refusal lines, the Mac hint --------------------

const Hosts = require("../../llm_switchboard/desk/static/desk_hosts.js");
const badges = (node) => node.all((k) => k.getAttribute("data-testid") === "host-badge").map((k) => k.textContent);
const lines = (h, id) => h.$(id).children.map((k) => k.textContent);
const DOWN = { host: "mac", state: "down", since: null, signer: true };

test("each target carries its host badge, and the detail shows it", async () => {
  const h = await paired(snap({ targets: [target("s1"), onMac({ session_id: "s2" })] }));
  assert.deepEqual(badges(h.target("s1")), ["Pi"]);
  assert.deepEqual(badges(h.target("s2")), ["mac"]);
  h.target("s2").click();
  await settle();
  assert.equal(h.$("detail-host").textContent, "mac");
});

test("one line per listed host and one per unattributed refusal, none when the hub lists none", async () => {
  const h = await paired(snap());
  assert.equal(h.$("links").hidden, true);
  assert.equal(h.$("link-refusals").hidden, true);
  await h.answer(POLL, 200, snap({ links: [{ host: "mac", state: "up", since: 1, signer: true }],
    admission_refusals: [{ code: "not_authorized", count: 2, last: 1, versions: null, hub_versions: [1] }] }));
  assert.equal(h.$("links").hidden, false);
  assert.deepEqual(lines(h, "links"), ["macMac link up"]);
  assert.deepEqual(badges(h.$("links")), ["mac"]);
  assert.equal(h.$("link-refusals").hidden, false);
  assert.match(lines(h, "link-refusals")[0], /^A Mac link was refused: not_authorized, 2 times, last at /);
  assert.deepEqual(badges(h.$("link-refusals")), [], "a refusal was shown under a host");
});

test("a Mac target with no registration is not connected, with the hint and its link's state, and takes no sends", async () => {
  const h = await paired(snap({ targets: [onMac({ registration: "none", generation: null })], links: [DOWN] }));
  assert.equal(h.$("detail-badge").textContent, "Not connected");
  assert.equal(h.$("detail-hint").hidden, false);
  assert.equal(h.$("detail-hint").textContent, Hosts.MAC_HINT + " Mac link down.");
  for (const id of ["dictate", "review-btn", "test-btn", "draft"]) assert.equal(h.$(id).disabled, true, id);
  await h.answer(POLL, 200, snap({ targets: [target("s1", { registration: "none", generation: null })] }));
  h.target("s1").click();
  await settle();
  assert.equal(h.$("detail-badge").textContent, "Unavailable");
  assert.equal(h.$("detail-hint").hidden, true);
});

test("an id two hosts own is badged, and the page offers no send to it", async () => {
  const h = await paired(snap({ targets: [target("s1", { enrolled: false, enrolled_on_two_hosts: true,
    registration: "none", generation: null })] }));
  assert.equal(h.$("detail-badge").textContent, "Enrolled on two hosts");
  assert.equal(h.$("review-btn").disabled, true);
});
