import test from "node:test";
import assert from "node:assert/strict";
import { deviceClass, dirKey, listed } from "../../llm_switchboard/static/dirs.js";

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
  assert.equal(dirKey("m4mac", "~/dev/obd-things"), "m4mac:~/dev/obd-things");
  assert.notEqual(deviceClass(""), deviceClass("m4mac"));
});
