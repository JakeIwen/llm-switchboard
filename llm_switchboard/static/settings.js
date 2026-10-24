// Keys and validation mirror settings.py; the server remains authoritative.
import { api } from "./api.js";
import { formDialog } from "./dialog.js";
import { toast } from "./feedback.js";

const COMMAND = /^[A-Za-z0-9_./-]{1,64}$/;
const HIDDEN = /^~(\/[^\s]+)?$/;
const FIELDS = [
  { name: "claude_command", label: "Claude command", command: true },
  { name: "codex_command", label: "Codex command", command: true },
  { name: "folders.depth", label: "Folder menu depth", range: [0, 6] },
  { name: "folders.min_sessions", label: "Minimum sessions per folder", range: [1, 50] },
  { name: "folders.hidden", label: "Hidden folders (comma-separated)", placeholder: "~/private, ~/archive" },
  { name: "poll_seconds", label: "Refresh interval (seconds)", range: [2, 60] },
];
const REFRESH_RANGE = FIELDS.find((field) => field.name === "poll_seconds").range;
const DEFAULT_POLL_SECONDS = 4;

export function hiddenFolders(text) {
  return text.split(",").map((part) => part.trim()).filter(Boolean);
}

export function settingsValues(settings) {
  return {
    claude_command: settings.claude_command,
    codex_command: settings.codex_command,
    "folders.depth": String(settings.folders.depth),
    "folders.min_sessions": String(settings.folders.min_sessions),
    "folders.hidden": settings.folders.hidden.join(", "),
    poll_seconds: String(settings.poll_seconds),
  };
}

export function settingsUpdate(values) {
  return {
    claude_command: values.claude_command,
    codex_command: values.codex_command,
    folders: {
      depth: Number(values["folders.depth"]),
      min_sessions: Number(values["folders.min_sessions"]),
      hidden: hiddenFolders(values["folders.hidden"]),
    },
    poll_seconds: Number(values.poll_seconds),
  };
}

export function settingsProblem(values) {
  for (const field of FIELDS) {
    const value = values[field.name];
    if (field.command && (!COMMAND.test(value) || /\s/.test(value)))
      return `${field.name} must be one shell word (1–64 letters, digits, _, ., / or -).`;
    if (field.range) {
      const [low, high] = field.range;
      if (!/^\d+$/.test(value) || Number(value) < low || Number(value) > high)
        return `${field.name} must be an integer from ${low} to ${high}.`;
    }
  }
  const hidden = hiddenFolders(values["folders.hidden"]);
  if (hidden.length > 50 || hidden.some((path) => !HIDDEN.test(path)))
    return "folders.hidden must contain at most 50 home-relative paths (~ or ~/folder), without whitespace.";
  return "";
}

export function pollIntervalMs(settings) {
  const seconds = settings?.poll_seconds;
  const [low, high] = REFRESH_RANGE;
  return (Number.isInteger(seconds) && seconds >= low && seconds <= high ? seconds : DEFAULT_POLL_SECONDS) * 1000;
}

export async function openSettings(refresh) {
  let values;
  try { values = settingsValues((await api("/api/settings")).settings); }
  catch (error) { return toast(`Couldn't load settings: ${error.message}`); }
  let serverError = "";
  while (true) {
    const entered = await formDialog({
      title: "Settings", confirm: "Save",
      body: `These settings apply to this install, in all browsers.${serverError ? `\n${serverError}` : ""}`,
      fields: FIELDS.map((field) => ({ ...field, value: values[field.name] })),
      validate: settingsProblem,
    });
    if (!entered) return;
    values = entered;
    try { await api("/api/settings", settingsUpdate(values)); }
    catch (error) {
      // Reopen with the raw field values, not a reload that would discard edits.
      serverError = `Couldn't save settings: ${error.message}`;
      toast(serverError);
      continue;
    }
    await refresh();
    toast("Settings saved for this install.");
    return;
  }
}
