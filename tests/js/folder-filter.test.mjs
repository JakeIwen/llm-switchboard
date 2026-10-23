import test from "node:test";
import assert from "node:assert/strict";
import { readFolders } from "../../llm_switchboard/static/folder-filter.js";

const storage = (values) => ({ getItem: (key) => values[key] ?? null });

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
