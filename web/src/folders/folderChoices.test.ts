import { test } from "vitest";
import assert from "node:assert/strict";
import { deviceClass, dirKey, folderChoices, listed, machineKey, UNKNOWN_DIR } from "./folderChoices";

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

test("a folder with one session, or a hidden prefix and below, doesn't", () => {
  assert.ok(!listed("~/dev/openwrt", 1));
  assert.ok(!listed("~/dev/openwrt", 0));
  for (const p of ["~/ais", "~/ais/", "~/ais/form-engine"]) assert.ok(!listed(p, 9, { hidden: ["~/ais"] }), p);
  // Nothing is hidden until the settings say so.
  assert.ok(listed("~/ais", 9));
});

test("the same folder on two machines gets two keys and two tints", () => {
  assert.equal(dirKey("", "~/dev/obd-things"), "~/dev/obd-things");
  assert.equal(dirKey(undefined, "~/dev/obd-things"), "~/dev/obd-things");
  assert.equal(dirKey("mac", "~/dev/obd-things"), "mac:~/dev/obd-things");
  assert.notEqual(deviceClass(""), deviceClass("mac"));
});

const sessionsIn = (project: string | undefined, count: number, host = "") => Array.from({ length: count }, (_, i) => ({
  project, host, title: `Session ${i}`, enabled: i === 0, tool: i % 2 ? "codex" : "claude",
}));

test("the default total adds only offered folders using all their sessions", () => {
  const sessions = [
    ...sessionsIn("~/dev/project", 4), ...sessionsIn("~", 2),
    ...sessionsIn("~/dev/scratch", 1), ...sessionsIn("~/dev/project/worktree", 8),
    ...sessionsIn("~/ais", 7), ...sessionsIn("~/ais/agent", 3),
    ...sessionsIn("/tmp/project", 6), ...sessionsIn(undefined, 5),
  ];
  const { choices, total } = folderChoices(sessions, [], { hidden: ["~/ais"] });
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

test("selected hidden and missing folders stay removable and contribute their matching counts", () => {
  const selected = new Set(["~/dev/project/worktree", "~/dev/scratch", "mac:~/dev/gone", "~/dev/local:gone", UNKNOWN_DIR]);
  const { choices, total } = folderChoices([
    ...sessionsIn("~/dev/project", 2), ...sessionsIn("~/dev/project/worktree", 8),
    ...sessionsIn("~/dev/scratch", 1), ...sessionsIn(undefined, 4),
  ], selected);
  assert.equal(total, 15);
  assert.equal(total, choices.reduce((sum, folder) => sum + folder.count, 0));
  assert.equal(choices.length, 6);
  for (const key of selected) assert.ok(choices.some((folder) => folder.key === key && !folder.defaultListed), key);
  assert.deepEqual(choices.find((folder) => folder.key === "mac:~/dev/gone"), {
    key: "mac:~/dev/gone", host: "mac", project: "~/dev/gone", count: 0, defaultListed: false,
  });
  assert.equal(choices.find((folder) => folder.key === "~/dev/local:gone")!.host, "");
  assert.equal(choices.find((folder) => folder.key === "~/dev/project/worktree")!.count, 8);
  assert.equal(choices.find((folder) => folder.key === UNKNOWN_DIR)!.count, 4);
});

test("folder eligibility accepts depth, minimum sessions and hidden-prefix settings", () => {
  assert.ok(listed("~/dev/project/worktree", 1, { depth: 3, minSessions: 1 }));
  assert.ok(!listed("~/dev/project", 8, { depth: 1 }));
  assert.ok(!listed("~/dev/project", 2, { minSessions: 3 }));
  assert.ok(listed("~/ais", 2, { hidden: [] }));
  assert.ok(!listed("~/dev/private/project", 9, { depth: 3, hidden: ["~/dev/private"] }));
  assert.ok(listed("~/dev/privately", 9, { hidden: ["~/dev/private"] }));
  assert.ok(listed("~", 2, { depth: 0 }));
  assert.ok(!listed("~/dev", 2, { depth: 0 }));
  const sessions = [...sessionsIn("~/dev/project/worktree", 1), ...sessionsIn("~/ais", 2)];
  assert.equal(folderChoices(sessions, [], { hidden: ["~/ais"] }).total, 0);
  assert.equal(folderChoices(sessions).total, 2);
  assert.equal(folderChoices(sessions, [], { depth: 3, minSessions: 1, hidden: [] }).total, 3);
});

test("counts follow matching sessions, and a folder with none is hidden unless picked", () => {
  const sessions = [...sessionsIn("~/dev/alpha", 4), ...sessionsIn("~/dev/beta", 3), ...sessionsIn("~/dev/scratch", 1)];
  const matching = sessions.filter((session) => session.tool === "codex" && session.project?.includes("alpha"));
  const filtered = folderChoices(sessions, [], {}, matching);
  assert.deepEqual(filtered.choices.map(({ key, count }) => [key, count]), [["~/dev/alpha", 2]]);
  assert.equal(filtered.total, 2);
  assert.deepEqual(folderChoices(sessions, [], {}, []).choices, []);
  // A picked folder stays, so it can be unpicked.
  assert.deepEqual(folderChoices(sessions, ["~/dev/beta"], {}, matching).choices.map(({ key, count }) => [key, count]),
    [["~/dev/alpha", 2], ["~/dev/beta", 0]]);
});

test("each machine with matching sessions is a place of its own", () => {
  const sessions = [...sessionsIn("~/dev/alpha", 3), ...sessionsIn("~/dev/alpha", 2, "mac"), ...sessionsIn("/tmp/x", 1, "mac")];
  const places = (matching: typeof sessions, selected: string[] = []) =>
    folderChoices(sessions, selected, {}, matching).machines.map(({ key, host, count }) => [key, host, count]);
  assert.deepEqual(places(sessions), [["@", "", 3], ["@mac", "mac", 3]]);
  const local = sessions.filter((session) => !session.host);
  assert.deepEqual(places(local), [["@", "", 3]]);
  assert.deepEqual(places(local, [machineKey("mac")]), [["@", "", 3], ["@mac", "mac", 0]]);
  // A picked machine is not mistaken for a folder.
  assert.deepEqual(folderChoices(sessions, ["@mac"], {}, local).choices.map(({ key }) => key), ["~/dev/alpha"]);
});

test("an empty catalogue has no default total and deduplicates retained selections", () => {
  assert.deepEqual(folderChoices([]), { choices: [], machines: [], total: 0 });
  assert.deepEqual(folderChoices([], ["~/dev/gone", "~/dev/gone"]), {
    choices: [{ key: "~/dev/gone", host: "", project: "~/dev/gone", count: 0, defaultListed: false }], machines: [], total: 0,
  });
});

test("matching rows outside the catalogue are ignored and prefix matching keeps legacy slashes", () => {
  const sessions = sessionsIn("~/dev/project", 2);
  assert.equal(folderChoices(sessions, [], {}, [...sessions, { project: "~/other" }]).total, 2);
  assert.ok(!listed("~", 3, { hidden: ["~"] }));
  assert.ok(!listed("~/dev", 3, { hidden: ["~"] }));
  assert.ok(listed("~/dev/private", 3, { hidden: ["~/dev/"] }));
});
