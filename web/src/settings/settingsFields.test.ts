import { test } from "vitest";
import assert from "node:assert/strict";
import { hiddenFolders, pollIntervalMs, settingsProblem, settingsUpdate, settingsValues } from "./settingsFields";

const defaults = {
  claude_command: "claude", codex_command: "codex",
  folders: { depth: 2, min_sessions: 2, hidden: [] }, poll_seconds: 4,
};

test("comma-separated hidden folders trim whitespace and ignore empty entries", () => {
  assert.deepEqual(hiddenFolders(" ~/private, , ~/archive/sub, "), ["~/private", "~/archive/sub"]);
  assert.deepEqual(hiddenFolders(""), []);
});

test("settings fields round trip without losing depth zero or empty hidden folders", () => {
  const settings = { ...defaults, folders: { ...defaults.folders, depth: 0 } };
  const values = settingsValues(settings);
  assert.equal(values["folders.depth"], "0");
  assert.equal(settingsProblem(values), "");
  assert.deepEqual(settingsUpdate(values), settings);
  assert.deepEqual(settingsUpdate({ ...values, "folders.hidden": "~/a, ~/b" }).folders.hidden, ["~/a", "~/b"]);
});

test("validation identifies invalid commands, ranges and hidden prefixes", () => {
  for (const [key, value] of [
    ["claude_command", "one two"], ["claude_command", "a\n"], ["codex_command", "a;true"],
    ["folders.depth", "7"], ["folders.depth", "-1"], ["folders.depth", ""],
    ["folders.min_sessions", "0"], ["folders.min_sessions", "2.5"],
    ["poll_seconds", "61"], ["poll_seconds", "NaN"], ["poll_seconds", "4e0"],
    ["folders.hidden", "/tmp"], ["folders.hidden", "~/two words"],
    ["folders.hidden", Array(51).fill("~/a").join(",")],
  ]) {
    assert.ok(settingsProblem({ ...settingsValues(defaults), [key]: value }).includes(key), `${key}=${value}`);
  }
});

test("poll intervals use current settings with the legacy four-second fallback", () => {
  for (const value of [undefined, {}, { poll_seconds: null }, { poll_seconds: 0 }, { poll_seconds: 61 }, { poll_seconds: "2" }]) {
    assert.equal(pollIntervalMs(value), 4000);
  }
  assert.equal(pollIntervalMs({ poll_seconds: 2 }), 2000);
  assert.equal(pollIntervalMs({ poll_seconds: 60 }), 60000);
});

test("inclusive limits and permitted shell-word characters remain accepted", () => {
  const values = settingsValues(defaults);
  for (const command of ["a", "a".repeat(64), "../bin/claude-name_1.2", "-", "/"]) {
    assert.equal(settingsProblem({ ...values, claude_command: command }), "");
  }
  for (const command of ["", "a".repeat(65), "a\\b", "é", "a\t"]) {
    assert.ok(settingsProblem({ ...values, codex_command: command }).startsWith("codex_command"));
  }
  for (const update of [
    { "folders.depth": "6" }, { "folders.min_sessions": "50" }, { poll_seconds: "2" },
    { poll_seconds: "60" }, { "folders.hidden": "~" },
    { "folders.hidden": Array(50).fill("~/a").join(",") }, { "folders.depth": "00" },
  ]) assert.equal(settingsProblem({ ...values, ...update }), "");
  for (const seconds of [NaN, Infinity, 2.5, -1, true, null]) assert.equal(pollIntervalMs({ poll_seconds: seconds }), 4000);
});

test("validation, conversion and polling match the old page", async () => {
  const url = new URL('../../../llm_switchboard/static/settings.js', import.meta.url).href;
  const legacy = await import(url);
  const values = settingsValues(defaults);
  for (const update of [{}, { claude_command: "bad command" }, { "folders.hidden": "~/a\n" },
    { "folders.hidden": "~/a,~,~/a" }, { "folders.depth": "006" }, { poll_seconds: "2.5" }]) {
    const input = { ...values, ...update };
    assert.equal(settingsProblem(input), legacy.settingsProblem(input));
    assert.deepEqual(settingsUpdate(input), legacy.settingsUpdate(input));
  }
  for (const poll_seconds of [undefined, null, 0, 2, 60, 61, "2", NaN]) {
    assert.equal(pollIntervalMs({ poll_seconds }), legacy.pollIntervalMs({ poll_seconds }));
  }
});
