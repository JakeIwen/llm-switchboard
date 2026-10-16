import test from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_SORT, SORTS, sortRows } from "../../llm_switchboard/static/sort.js";

const rows = [
  { id: "a", title: "beta notes", project: "~/dev/beta", mtime: 300, size: 10, enabled: false },
  { id: "b", title: "Alpha decoder", project: "~/dev/alpha", mtime: 200, size: 50, enabled: true },
  { id: "c", title: "alpha 10", project: "~/dev/alpha", mtime: 400, size: null, enabled: false },
  { id: "d", title: "alpha 9", project: "~/dev/beta", mtime: 100, size: 50, enabled: true },
];
const ids = (key) => sortRows(rows, key).map((r) => r.id).join("");

test("default is kept first, then most recent", () => {
  assert.equal(DEFAULT_SORT, "kept");
  assert.equal(ids("kept"), "bdca");
  assert.equal(ids(undefined), "bdca");
  assert.equal(ids("no-such-sort"), "bdca");
});

test("activity both ways", () => {
  assert.equal(ids("recent"), "cabd");
  assert.equal(ids("oldest"), "dbac");
});

test("name ignores case and sorts numbers naturally", () => {
  assert.equal(ids("name"), "dcba");
});

test("folder groups, recent within; size puts unknown last and breaks ties by activity", () => {
  assert.equal(ids("folder"), "cbad");
  assert.equal(ids("size"), "bdac");
});

test("sorting copies and leaves the input alone", () => {
  const before = rows.map((r) => r.id).join("");
  sortRows(rows, "name");
  assert.equal(rows.map((r) => r.id).join(""), before);
  assert.deepEqual(SORTS.map((s) => s.key), ["kept", "recent", "oldest", "name", "folder", "size"]);
});
