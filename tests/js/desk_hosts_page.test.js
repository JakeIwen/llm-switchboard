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

// ---- Candidates, Enroll now, the host choice, unenroll --------------------

const CANDIDATE = { host: "mac", session_id: "c1-0000", label: "asking", folder: "dev/x", seen: 1 };
const MAC_LINK = { host: "mac", state: "up", since: 1, signer: true };
const byTestid = (node, id) => node.all((k) => k.getAttribute("data-testid") === id);
const tap = async (node, id, n) => { byTestid(node, id)[n || 0].click(); await settle(); };

test("a candidate is listed with Enroll now, which asks first and then enrolls on its host", async () => {
  const h = await paired(snap({ candidates: [CANDIDATE], links: [MAC_LINK] }));
  const list = h.$("candidate-list");
  assert.equal(h.$("candidates").hidden, false);
  assert.deepEqual(badges(list), ["mac"]);
  assert.match(list.textContent, /asking/);
  assert.match(list.textContent, /dev\/x/);
  await tap(list, "enroll-now");
  assert.equal(h.pending("/desk/api/enroll-host").length, 0, "enrolled before the confirmation");
  assert.equal(byTestid(list, "enroll-now-text")[0].textContent, Hosts.ENROLL_NOW_NOTICE);
  assert.equal(byTestid(list, "enroll-now-yes")[0].textContent, "Enroll now");
  await tap(list, "enroll-now-yes");
  const [call] = h.pending("/desk/api/enroll-host");
  assert.deepEqual(call.body, { session_id: "c1-0000", host: "mac" });
  await h.answer("/desk/api/enroll-host", 200, { session_id: "c1-0000", host: "mac", enrolled: true });
  assert.equal(h.$("candidates-note").textContent, "Enrolled.");
  await h.answer(POLL, 200, snap({ links: [MAC_LINK] }));
  assert.equal(h.$("candidates").hidden, false, "the outcome went with the candidate");
});

test("Cancel, or the candidate leaving the list, drops the confirmation without an edit", async () => {
  const h = await paired(snap({ candidates: [CANDIDATE] }));
  await tap(h.$("candidate-list"), "enroll-now");
  await tap(h.$("candidate-list"), "enroll-now-no");
  assert.equal(byTestid(h.$("candidate-list"), "enroll-now-text").length, 0);
  await tap(h.$("candidate-list"), "enroll-now");
  await h.answer(POLL, 200, snap());
  assert.equal(h.$("candidates").hidden, true);
  await h.answer(POLL, 200, snap({ candidates: [CANDIDATE] }));
  assert.equal(byTestid(h.$("candidate-list"), "enroll-now-text").length, 0, "the confirmation outlived its candidate");
  assert.equal(h.all("/desk/api/enroll-host").length, 0);
});

test("the typed form's host choice: pi and the hosts in links; a Mac host asks Enroll now first", async () => {
  const h = await paired(snap({ links: [MAC_LINK] }));
  const select = h.$("enroll-host");
  assert.deepEqual(select.children.map((k) => k.getAttribute("value")), ["pi", "mac"]);
  assert.equal(select.value, "pi");
  h.$("enroll-input").value = "s9";
  h.$("enroll-form").dispatch("submit");
  await settle();
  assert.deepEqual(h.pending("/desk/api/enroll")[0].body, { session_id: "s9" });
  select.value = "mac";
  h.$("enroll-input").value = "s8";
  h.$("enroll-form").dispatch("submit");
  await settle();
  const confirm = h.$("enroll-form-confirm");
  assert.equal(byTestid(confirm, "enroll-now-text")[0].textContent, Hosts.ENROLL_NOW_NOTICE);
  await tap(confirm, "enroll-now-yes");
  assert.deepEqual(h.pending("/desk/api/enroll-host")[0].body, { session_id: "s8", host: "mac" });
  await h.answer(POLL, 200, snap({ links: [MAC_LINK] }));
  assert.equal(select.value, "mac", "the chosen host was not kept");
  await h.answer(POLL, 200, snap());
  assert.equal(select.value, "pi");
});

test("a Mac target unenrolls on its host; a phase 1 hub's 404 says it is older", async () => {
  const h = await paired(snap({ targets: [target("s1"), onMac({ session_id: "s2" })] }));
  const row = h.$("enroll-list").all((k) => k.getAttribute("data-host") === "mac")[0];
  assert.deepEqual(badges(row), ["mac"]);
  await tap(row, "unenroll-btn");
  assert.deepEqual(h.pending("/desk/api/unenroll-host")[0].body, { session_id: "s2", host: "mac" });
  await h.answer("/desk/api/unenroll-host", 404, { error: "not found" });
  assert.equal(h.$("settings-note").textContent, Hosts.OLDER_HUB);
});

test("an id two hosts own offers an unenroll on each host; an unenrolled Mac target offers Enroll now", async () => {
  const h = await paired(snap({ links: [MAC_LINK], targets: [
    target("s1", { enrolled: false, enrolled_on_two_hosts: true, registration: "none", generation: null }),
    onMac({ session_id: "s2", enrolled: false, lock: { lock_id: "k", request_id: "r", since: 1 } })] }));
  const rows = h.$("enroll-list").children;
  assert.deepEqual(byTestid(rows[0], "unenroll-btn").map((k) => k.textContent), ["Unenroll on Pi", "Unenroll on mac"]);
  await tap(rows[0], "unenroll-btn", 1);
  assert.deepEqual(h.pending("/desk/api/unenroll-host")[0].body, { session_id: "s1", host: "mac" });
  assert.deepEqual(byTestid(rows[1], "enroll-btn").map((k) => k.textContent), ["Enroll now"]);
  await tap(rows[1], "enroll-btn");
  assert.equal(h.all("/desk/api/enroll-host").length, 0);
  await tap(h.$("enroll-form-confirm"), "enroll-now-yes");
  assert.deepEqual(h.pending("/desk/api/enroll-host")[0].body, { session_id: "s2", host: "mac" });
});
