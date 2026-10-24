import test from "node:test";
import assert from "node:assert/strict";
import { FolderFilter, readFolders } from "../../llm_switchboard/static/folder-filter.js";

const storage = (values) => ({ getItem: (key) => values[key] ?? null });

function fixture(t) {
  class Element extends EventTarget {
    value = "";
    classList = { toggle() {} };
    setAttribute() {}
    querySelectorAll() { return []; }
    focus() {}
  }
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const document = globalThis.document, window = globalThis.window;
  globalThis.document = { getElementById: element };
  globalThis.window = new Element();
  t.after(() => { globalThis.document = document; globalThis.window = window; });
  const values = {};
  const saved = { ...storage(values), setItem: (key, value) => { values[key] = value; }, removeItem: (key) => { delete values[key]; } };
  const sessions = ["alpha", "alpha", "beta", "beta", "gamma", "gamma"].map((name) => ({ project: `~/dev/${name}` }));
  let matching = sessions;
  const filter = new FolderFilter({ storage: saved, setHTML() {}, onChange: () => filter.render(sessions, matching) });
  filter.render(sessions);
  return { filter, element, saved, match(rows) { matching = rows; filter.render(sessions, matching); }, sessions };
}

test("counts and fixed clear state follow other filters, not the selected folders", (t) => {
  const { filter, element, match, sessions, saved } = fixture(t);
  filter.set(["~/dev/alpha"]);
  match([sessions[0], sessions[2]]);
  assert.equal(element("dir-count").textContent, 1);
  assert.equal(element("folder-total").textContent, 2);
  assert.equal(element("folder-reset").hidden, false);
  match([]);
  assert.equal(element("dir-count").textContent, 0);
  assert.equal(element("folder-reset").hidden, false);
  assert.deepEqual([...readFolders(saved)], ["~/dev/alpha"]);
  element("folder-reset").dispatchEvent(new Event("click"));
  assert.equal(element("folder-reset").hidden, true);
  assert.equal(element("folder-selection").hidden, true);
  assert.deepEqual([...readFolders(saved)], []);
});

test("Invert toggles all visible choices and preserves selections outside the folder search", (t) => {
  const { filter, element, saved } = fixture(t);
  filter.set(["~/dev/alpha"]);
  element("folder-invert").dispatchEvent(new Event("click"));
  assert.deepEqual([...readFolders(saved)], ["~/dev/beta", "~/dev/gamma"]);
  element("folder-search").value = "beta";
  element("folder-search").dispatchEvent(new Event("input"));
  element("folder-invert").dispatchEvent(new Event("click"));
  assert.deepEqual([...readFolders(saved)], ["~/dev/gamma"]);
  element("folder-invert").dispatchEvent(new Event("click"));
  assert.deepEqual([...readFolders(saved)], ["~/dev/gamma", "~/dev/beta"]);
  element("folder-search").value = "nothing";
  element("folder-search").dispatchEvent(new Event("input"));
  assert.equal(element("folder-invert").disabled, true);
});

test("folder preference reads multiple choices and deduplicates them", () => {
  assert.deepEqual([...readFolders(storage({ "switchboard.dirs": '["~/dev/a","mac:~/dev/a","~/dev/a",null,2,""]' }))],
    ["~/dev/a", "mac:~/dev/a"]);
});

test("legacy folder preference remains selected until the new preference is saved", () => {
  assert.deepEqual([...readFolders(storage({ "switchboard.dir": "~/dev/old" }))], ["~/dev/old"]);
  assert.deepEqual([...readFolders(storage({ "switchboard.dirs": "[]", "switchboard.dir": "~/dev/old" }))], []);
});

test("invalid or unavailable storage leaves every folder available", () => {
  for (const saved of ["broken JSON", "{}", "null", '"~/dev/a"']) {
    assert.equal(readFolders(storage({ "switchboard.dirs": saved })).size, 0);
  }
  assert.equal(readFolders({ getItem() { throw new Error("blocked storage"); } }).size, 0);
  assert.equal(readFolders(undefined).size, 0);
});
