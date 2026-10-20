import test from "node:test";
import assert from "node:assert/strict";
import { hasChip } from "../../llm_switchboard/static/dirs.js";

test("home and up to two levels below it get a chip", () => {
  for (const p of ["~", "~/dev", "~/dev/llm-switchboard", "~/dev/llm-switchboard/",
                   "/Users/jacobr", "/Users/jacobr/dev/scripts", "/home/someone/dev/x"]) {
    assert.ok(hasChip(p), p);
  }
});

test("deeper folders and folders outside home don't", () => {
  for (const p of ["~/dev/scripts/macbook", "/Users/jacobr/dev/jri/.local/drafts/0a1b",
                   "/Users/jacobr/.codex/worktrees/694e/openwrt", "/tmp", "/private/tmp",
                   "/Volumes/pihome/dev/obd-things", "/Users", "~other/dev", "(unknown folder)", "", undefined]) {
    assert.ok(!hasChip(p), String(p));
  }
});
