import test from "node:test";
import assert from "node:assert/strict";
import { deviceClass, dirKey, hasChip } from "../../llm_switchboard/static/dirs.js";

test("home and up to two levels below it get a chip", () => {
  for (const p of ["~", "~/dev", "~/dev/llm-switchboard", "~/dev/llm-switchboard/"]) {
    assert.ok(hasChip(p), p);
  }
});

test("deeper folders and folders outside home don't", () => {
  for (const p of ["~/dev/scripts/macbook", "~/dev/jri/.local/drafts/0a1b", "~/.codex/worktrees/694e/openwrt",
                   "/tmp", "/private/tmp", "/Volumes/pihome/dev/obd-things", "/Users/Shared/x",
                   "~other/dev", "(unknown folder)", "", undefined]) {
    assert.ok(!hasChip(p), String(p));
  }
});

test("the same folder on two machines gets two keys and two tints", () => {
  assert.equal(dirKey("", "~/dev/obd-things"), "~/dev/obd-things");
  assert.equal(dirKey(undefined, "~/dev/obd-things"), "~/dev/obd-things");
  assert.equal(dirKey("m4mac", "~/dev/obd-things"), "m4mac:~/dev/obd-things");
  assert.notEqual(deviceClass(""), deviceClass("m4mac"));
});
