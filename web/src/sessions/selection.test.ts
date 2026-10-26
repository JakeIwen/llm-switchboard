import { test } from "vitest";
import assert from "node:assert/strict";
import { Selection } from "./selection";

const order = ["a", "b", "c", "d", "e"];
const sorted = (sel: Selection) => sel.list(order);

test("plain click selects one, clicking it again clears", () => {
  const sel = new Selection();
  sel.only("b"); sel.only("c");
  assert.deepEqual(sorted(sel), ["c"]);
  sel.only("c");
  assert.deepEqual(sorted(sel), []);
});

test("toggle adds and removes", () => {
  const sel = new Selection();
  sel.toggle("a"); sel.toggle("d"); sel.toggle("a");
  assert.deepEqual(sorted(sel), ["d"]);
});

test("shift ranges extend from the anchor, either direction", () => {
  const sel = new Selection();
  sel.only("b");
  sel.extendTo(order, "d");
  assert.deepEqual(sorted(sel), ["b", "c", "d"]);
  sel.extendTo(order, "a");
  assert.deepEqual(sorted(sel), ["a", "b"]);
});

test("cmd+shift keeps the existing selection", () => {
  const sel = new Selection();
  sel.toggle("a");
  sel.moveCursor("c");
  sel.extendTo(order, "e", true);
  assert.deepEqual(sorted(sel), ["a", "c", "d", "e"]);
});

test("arrow stepping clamps and starts at an end", () => {
  const sel = new Selection();
  assert.equal(sel.step(order, 1), "a");
  assert.equal(sel.step(order, -1), "e");
  sel.moveCursor("e");
  assert.equal(sel.step(order, 1), "e");
  assert.equal(sel.step(order, -1), "d");
});

test("prune drops hidden rows", () => {
  const sel = new Selection();
  sel.selectAll(order);
  sel.moveCursor("e");
  sel.prune(["a", "b"]);
  assert.deepEqual(sel.list(order), ["a", "b"]);
  assert.equal(sel.cursor, null);
});

test("missing range targets are ignored and absent anchors start a singleton range", () => {
  const sel = new Selection();
  sel.only("b");
  sel.extendTo(order, "missing");
  assert.deepEqual(sel.list(order), ["b"]);
  assert.equal(sel.cursor, "b");
  sel.moveCursor("missing");
  sel.extendTo(order, "d");
  assert.deepEqual(sel.list(order), ["d"]);
  assert.equal(sel.anchor, "d");
  const unanchored = new Selection();
  unanchored.extendTo(order, "c");
  assert.deepEqual(unanchored.list(order), ["c"]);
});

test("empty order, boundary stepping, clear and select-all retain anchor contracts", () => {
  const sel = new Selection();
  assert.equal(sel.step([], 1), null);
  sel.moveCursor("a");
  assert.equal(sel.step(order, -1), "a");
  assert.equal(sel.step(order, 99), "e");
  assert.equal(sel.step(order, 0), "a");
  sel.selectAll(order);
  assert.equal(sel.size, 5);
  assert.equal(sel.has("d"), true);
  assert.equal(sel.cursor, "a");
  sel.clear();
  assert.equal(sel.size, 0);
  assert.equal(sel.anchor, "a");
  assert.equal(sel.cursor, "a");
  sel.prune(["a"]);
  assert.equal(sel.anchor, "a");
  sel.prune([]);
  assert.equal(sel.anchor, null);
  assert.equal(sel.cursor, null);
});
