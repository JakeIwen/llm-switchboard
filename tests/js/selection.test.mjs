import test from "node:test";
import assert from "node:assert/strict";
import { Selection } from "../../llm_switchboard/static/selection.js";

const order = ["a", "b", "c", "d", "e"];
const sorted = (sel) => sel.list(order);

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
