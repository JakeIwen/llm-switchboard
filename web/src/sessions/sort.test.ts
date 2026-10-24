import { test } from "vitest";
import assert from "node:assert/strict";
import { DEFAULT_SORT, SORTS, sortRows } from "./sort";

const rows = [
  { id: "a", title: "beta notes", project: "~/dev/beta", mtime: 300, size: 10, enabled: false },
  { id: "b", title: "Alpha decoder", project: "~/dev/alpha", mtime: 200, size: 50, enabled: true },
  { id: "c", title: "alpha 10", project: "~/dev/alpha", mtime: 400, size: null, enabled: false },
  { id: "d", title: "alpha 9", project: "~/dev/beta", mtime: 100, size: 50, enabled: true },
];
const ids = (key: string | undefined) => sortRows(rows, key).map((r) => r.id).join("");

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

test("ties retain server order, absent activity is zero, and rows retain identity", () => {
  const tied = ["b", "a", "c"].map((id) => ({ id, title: "Same", project: "~", mtime: null, size: null, enabled: false }));
  for (const { key } of SORTS) {
    const sorted = sortRows(tied, key);
    assert.deepEqual(sorted.map((row) => row.id), ["b", "a", "c"]);
    assert.notEqual(sorted, tied);
    assert.equal(sorted[0], tied[0]);
  }
  assert.deepEqual(sortRows([], "kept"), []);
  assert.equal(ids("constructor"), "bdca");
});

test("every order matches the live page's pure module", async () => {
  const url = new URL('../../../llm_switchboard/static/sort.js', import.meta.url).href;
  const legacy = await import(url);
  for (const key of [...SORTS.map((sort) => sort.key), undefined, 'no-such-sort', '__proto__']) {
    assert.deepEqual(sortRows(rows, key), legacy.sortRows(rows, key));
  }
});
