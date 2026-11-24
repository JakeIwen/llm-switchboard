// desk_hosts.js: the Mac link's words on the tablet page (docs/voice-desk.md,
// "What the desk page shows"), without a DOM.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const STATIC = path.join(__dirname, "..", "..", "llm_switchboard", "desk", "static");
const Hosts = require(path.join(STATIC, "desk_hosts.js"));
const Logic = require(path.join(STATIC, "desk_logic.js"));

const AT = 1700000000;

test("a host badge reads Pi for the Pi's own sessions and the Mac's name otherwise", () => {
  assert.equal(Hosts.hostLabel("pi"), "Pi");
  assert.equal(Hosts.hostLabel("mac"), "mac");
});

test("one link line per state: up, down since a time or not, and the verifier the Pi lacks", () => {
  assert.deepEqual(Hosts.linkLine({ host: "mac", state: "up", since: AT, signer: true }),
    { text: "Mac link up", tone: "ok" });
  assert.deepEqual(Hosts.linkLine({ host: "mac", state: "down", since: AT, signer: true }),
    { text: "Mac link down since " + Logic.formatTime(AT), tone: "warn" });
  assert.deepEqual(Hosts.linkLine({ host: "mac", state: "down", since: null, signer: false }),
    { text: "Mac link down", tone: "warn" });
  assert.deepEqual(Hosts.linkLine({ host: "mac", state: "verifier_unavailable", since: null, signer: true }),
    { text: "Mac links unavailable: the Pi can't verify Mac keys (OpenSSH 8.1 or later is needed)", tone: "bad" });
});

test("unattributed refusals: the version mismatch names both sides, any other code its count", () => {
  const last = ", last at " + Logic.formatTime(AT);
  assert.equal(Hosts.refusalLine({ code: "version", count: 3, last: AT, versions: [2], hub_versions: [1] }),
    "A Mac link was refused: version mismatch (the relay speaks 2, the hub speaks 1; update both machines)" + last);
  assert.equal(Hosts.refusalLine({ code: "version", count: 1, last: AT, versions: [2, 3], hub_versions: [1] }),
    "A Mac link was refused: version mismatch (the relay speaks 2 or 3, the hub speaks 1; update both machines)" + last);
  // A version refusal whose relay list wasn't kept falls back to the count.
  assert.equal(Hosts.refusalLine({ code: "version", count: 2, last: AT, versions: null, hub_versions: [1] }),
    "A Mac link was refused: version, 2 times" + last);
  assert.equal(Hosts.refusalLine({ code: "not_authorized", count: 4, last: AT, versions: null, hub_versions: [1] }),
    "A Mac link was refused: not_authorized, 4 times" + last);
  assert.equal(Hosts.refusalLine({ code: "proof_failed", count: 1, last: AT, versions: null, hub_versions: [1] }),
    "A Mac link was refused: proof_failed, 1 time" + last);
  assert.deepEqual(Hosts.refusalLines({}), []);
  assert.deepEqual(Hosts.refusalLines(null), []);
});

test("the host choice is pi and each host in links, once", () => {
  assert.deepEqual(Hosts.hostChoices(null), ["pi"]);
  assert.deepEqual(Hosts.hostChoices({ links: [{ host: "mac" }, { host: "mac2" }, { host: "mac" }] }),
    ["pi", "mac", "mac2"]);
});

test("the Mac hint: only a Mac target without a registration, with its link's state", () => {
  const state = { links: [{ host: "mac", state: "down", since: null, signer: true }] };
  const mac = { host: "mac", session_id: "s1", registration: "none" };
  assert.equal(Hosts.macHint(mac, state), Hosts.MAC_HINT + " Mac link down.");
  assert.equal(Hosts.macHint(mac, { links: [] }), Hosts.MAC_HINT);
  assert.equal(Hosts.macHint(Object.assign({}, mac, { registration: "registered" }), state), "");
  assert.equal(Hosts.macHint(Object.assign({}, mac, { host: "pi" }), state), "");
  assert.equal(Hosts.macHint(Object.assign({}, mac, { enrolled_on_two_hosts: true }), state), "");
  assert.equal(Hosts.macHint(null, state), "");
});

test("an edit takes the Pi's route for pi and the host-qualified route for a Mac", () => {
  assert.deepEqual(Hosts.enrollRequest("enroll", "pi", "s1"), { path: "/desk/api/enroll", body: { session_id: "s1" } });
  assert.deepEqual(Hosts.enrollRequest("unenroll", "pi", "s1"),
    { path: "/desk/api/unenroll", body: { session_id: "s1" } });
  assert.deepEqual(Hosts.enrollRequest("enroll", "mac", "s1"),
    { path: "/desk/api/enroll-host", body: { session_id: "s1", host: "mac" } });
  assert.deepEqual(Hosts.enrollRequest("unenroll", "mac", "s1"),
    { path: "/desk/api/unenroll-host", body: { session_id: "s1", host: "mac" } });
});

test("an edit's answer: done, refused, a phase 1 hub's 404 on a Mac edit, or no answer", () => {
  assert.equal(Hosts.enrollOutcome({ status: 200, data: {} }, "enroll", "mac"), "Enrolled.");
  assert.equal(Hosts.enrollOutcome({ status: 200, data: {} }, "unenroll", "pi"), "Unenrolled.");
  assert.equal(Hosts.enrollOutcome({ status: 404, data: { error: "not found" } }, "enroll", "mac"), Hosts.OLDER_HUB);
  assert.equal(Hosts.enrollOutcome({ status: 404, data: { error: "not found" } }, "unenroll", "mac"), Hosts.OLDER_HUB);
  assert.equal(Hosts.enrollOutcome({ status: 404, data: { error: "not found" } }, "enroll", "pi"),
    "Failed: not found.");
  assert.equal(Hosts.enrollOutcome({ status: 409, data: { error: "enrolled on another host" } }, "enroll", "mac"),
    "Failed: enrolled on another host.");
  assert.equal(Hosts.enrollOutcome({ status: 502, data: null }, "enroll", "mac"), "Failed: HTTP 502.");
  assert.equal(Hosts.enrollOutcome(null, "enroll", "mac"), "Could not reach the hub.");
});

test("unenroll names the target's host, or every known host for an id two hosts own", () => {
  const state = { links: [{ host: "mac" }] };
  assert.deepEqual(Hosts.unenrollHosts({ host: "mac", enrolled: true }, state), ["mac"]);
  assert.deepEqual(Hosts.unenrollHosts({ host: "pi", enrolled: false }, state), []);
  assert.deepEqual(Hosts.unenrollHosts({ host: "pi", enrolled: false, enrolled_on_two_hosts: true }, state),
    ["pi", "mac"]);
});
