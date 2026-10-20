// The folder bar and folder paths: one chip per folder per machine, for home
// and up to two levels below it, so one-off scratch, worktree and draft folders
// don't crowd it out. Their sessions still show under All folders and match
// dir: searches. Each machine's paths get a light tint of their own.

export const CHIP_DEPTH = 2;
// Both the Pi's and the Mac's home folders arrive shortened to ~
// (transcripts.display_path, hosts.host_display_path).
const UNDER_HOME = /^~(\/.*)?$/;

export function hasChip(project) {
  const m = UNDER_HOME.exec(project || "");
  return Boolean(m) && (m[1] || "").split("/").filter(Boolean).length <= CHIP_DEPTH;
}

// A folder's key in the bar and the dir filter. Another machine's folders carry
// its name, so the Pi's ~/dev/x and the Mac's ~/dev/x stay apart.
export const dirKey = (host, project) => host ? `${host}:${project}` : project;

// The class that tints a folder path by machine, and the tooltip naming it.
export const deviceClass = (host) => host ? "dev-host" : "dev-local";
export const deviceTitle = (host) => host ? `On ${host}` : "On this Pi";
