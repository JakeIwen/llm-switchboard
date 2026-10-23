import test from "node:test";
import assert from "node:assert/strict";
import { deviceClass, dirKey, folderChoices, listed, UNKNOWN_DIR } from "../../llm_switchboard/static/dirs.js";

test("home and up to two levels below it are listed", () => {
  for (const p of ["~", "~/dev", "~/dev/llm-switchboard", "~/dev/llm-switchboard/", "~/aisle"]) {
    assert.ok(listed(p, 2), p);
  }
});

test("deeper folders and folders outside home don't", () => {
  for (const p of ["~/dev/scripts/macbook", "~/dev/jri/.local/drafts/0a1b", "~/.codex/worktrees/694e/openwrt",
                   "/tmp", "/private/tmp", "/Volumes/pihome/dev/obd-things", "/Users/Shared/x",
                   "~other/dev", "(unknown folder)", "", undefined]) {
    assert.ok(!listed(p, 50), String(p));
  }
});

test("a folder with one session, or ~/ais and below, doesn't", () => {
  assert.ok(!listed("~/dev/openwrt", 1));
  assert.ok(!listed("~/dev/openwrt", 0));
  for (const p of ["~/ais", "~/ais/", "~/ais/form-engine"]) assert.ok(!listed(p, 9), p);
});

test("the same folder on two machines gets two keys and two tints", () => {
  assert.equal(dirKey("", "~/dev/obd-things"), "~/dev/obd-things");
  assert.equal(dirKey(undefined, "~/dev/obd-things"), "~/dev/obd-things");
  assert.equal(dirKey("mac", "~/dev/obd-things"), "mac:~/dev/obd-things");
  assert.notEqual(deviceClass(""), deviceClass("mac"));
});

const sessionsIn = (project, count, host = "") => Array.from({ length: count }, (_, i) => ({
  project, host, title: `Session ${i}`, enabled: i === 0, tool: i % 2 ? "codex" : "claude",
}));

test("the default total adds only offered folders using all their sessions", () => {
  const sessions = [
    ...sessionsIn("~/dev/project", 4), ...sessionsIn("~", 2),
    ...sessionsIn("~/dev/scratch", 1), ...sessionsIn("~/dev/project/worktree", 8),
    ...sessionsIn("~/ais", 7), ...sessionsIn("~/ais/agent", 3),
    ...sessionsIn("/tmp/project", 6), ...sessionsIn(undefined, 5),
  ];
  const { choices, total } = folderChoices(sessions);
  assert.equal(total, 6);
  assert.deepEqual(choices.map(({ project, count }) => [project, count]), [["~", 2], ["~/dev/project", 4]]);
  assert.ok(choices.every((folder) => folder.defaultListed));
  assert.equal(total, choices.reduce((sum, folder) => sum + folder.count, 0));
});

test("folder counts and ordering keep identical paths on different machines separate", () => {
  const { choices, total } = folderChoices([
    ...sessionsIn("~/dev/project10", 2, "zhost"),
    ...sessionsIn("~/dev/project10", 2),
    ...sessionsIn("~/dev/project2", 3, "mac"),
    ...sessionsIn("~/dev/project2", 2),
    ...sessionsIn("~/dev/project10", 4, "mac"),
  ]);
  assert.equal(total, 13);
  assert.deepEqual(choices.map(({ key, count }) => [key, count]), [
    ["~/dev/project2", 2], ["~/dev/project10", 2],
    ["mac:~/dev/project2", 3], ["mac:~/dev/project10", 4],
    ["zhost:~/dev/project10", 2],
  ]);
});

test("selected hidden and missing folders stay removable without raising the default total", () => {
  const selected = new Set(["~/dev/project/worktree", "~/dev/scratch", "mac:~/dev/gone", "~/dev/local:gone", UNKNOWN_DIR]);
  const { choices, total } = folderChoices([
    ...sessionsIn("~/dev/project", 2), ...sessionsIn("~/dev/project/worktree", 8),
    ...sessionsIn("~/dev/scratch", 1), ...sessionsIn(undefined, 4),
  ], selected);
  assert.equal(total, 2);
  assert.equal(choices.length, 6);
  for (const key of selected) assert.ok(choices.some((folder) => folder.key === key && !folder.defaultListed), key);
  assert.deepEqual(choices.find((folder) => folder.key === "mac:~/dev/gone"), {
    key: "mac:~/dev/gone", host: "mac", project: "~/dev/gone", count: 0, defaultListed: false,
  });
  assert.equal(choices.find((folder) => folder.key === "~/dev/local:gone").host, "");
  assert.equal(choices.find((folder) => folder.key === "~/dev/project/worktree").count, 8);
  assert.equal(choices.find((folder) => folder.key === UNKNOWN_DIR).count, 4);
});

test("an empty catalogue has no default total and deduplicates retained selections", () => {
  assert.deepEqual(folderChoices([]), { choices: [], total: 0 });
  assert.deepEqual(folderChoices([], ["~/dev/gone", "~/dev/gone"]), {
    choices: [{ key: "~/dev/gone", host: "", project: "~/dev/gone", count: 0, defaultListed: false }], total: 0,
  });
});
