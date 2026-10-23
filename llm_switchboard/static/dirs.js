// The folder menu and folder paths: one entry per folder per machine, for home
// and up to two levels below it with at least two sessions, so one-off scratch,
// worktree and draft folders don't crowd it out. Their sessions still show
// under All folders and match dir: searches. Each machine's paths get a light
// tint of their own.

export const CHIP_DEPTH = 2;
export const MIN_SESSIONS = 2;
export const UNKNOWN_DIR = "(unknown folder)";
// Folders never listed, with everything under them.
const NO_CHIP = ["~/ais"];
// Both the Pi's and the Mac's home folders arrive shortened to ~
// (transcripts.display_path, hosts.host_display_path).
const UNDER_HOME = /^~(\/.*)?$/;

// sessions: all of the folder's sessions on that machine, whatever the search,
// so entries don't come and go while typing.
export function listed(project, sessions) {
  const m = UNDER_HOME.exec(project || "");
  if (!m || sessions < MIN_SESSIONS || NO_CHIP.some((p) => project === p || project.startsWith(p + "/"))) return false;
  return (m[1] || "").split("/").filter(Boolean).length <= CHIP_DEPTH;
}

// A folder's key in the menu and the dir filter. Another machine's folders carry
// its name, so the Pi's ~/dev/x and the Mac's ~/dev/x stay apart.
export const dirKey = (host, project) => host ? `${host}:${project}` : project;

// Count the unfiltered catalogue, so search and status changes do not change
// which folders can be chosen. Selected exceptions stay removable without
// inflating the total for the default choices.
export function folderChoices(sessions, selected = []) {
  const picked = new Set(selected);
  const folders = new Map();
  for (const session of sessions) {
    const host = session.host || "";
    const project = session.project || UNKNOWN_DIR;
    const key = dirKey(host, project);
    const folder = folders.get(key) || { key, host, project, count: 0, defaultListed: false };
    folder.count++;
    folders.set(key, folder);
  }
  let total = 0;
  for (const folder of folders.values()) {
    folder.defaultListed = listed(folder.project, folder.count);
    if (folder.defaultListed) total += folder.count;
  }
  for (const key of picked) {
    if (folders.has(key)) continue;
    // Host names in the exchange are lowercase letters, digits and hyphens.
    const remote = /^([a-z0-9-]{1,32}):(.*)$/.exec(key);
    folders.set(key, {
      key, host: remote ? remote[1] : "", project: remote ? remote[2] : key,
      count: 0, defaultListed: false,
    });
  }
  const choices = [...folders.values()].filter((folder) => folder.defaultListed || picked.has(folder.key));
  choices.sort((a, b) => (a.host !== "") - (b.host !== "")
    || a.host.localeCompare(b.host)
    || a.project.localeCompare(b.project, undefined, { sensitivity: "base", numeric: true })
    || a.project.localeCompare(b.project));
  return { choices, total };
}

// The class that tints a folder path by machine, and the tooltip naming it.
export const deviceClass = (host) => host ? "dev-host" : "dev-local";
export const deviceTitle = (host) => host ? `On ${host}` : "On this Pi";
