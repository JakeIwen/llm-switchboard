// Which folders get a chip in the folder bar: home and up to two levels below
// it, so one-off scratch, worktree and draft folders don't crowd it out. Their
// sessions still show under All folders and match dir: searches.

export const CHIP_DEPTH = 2;
// Pi rows arrive with the home folder shortened to ~; Mac rows carry full paths.
const UNDER_HOME = /^(?:~|\/Users\/[^/]+|\/home\/[^/]+)(\/.*)?$/;

export function hasChip(project) {
  const m = UNDER_HOME.exec(project || "");
  return Boolean(m) && (m[1] || "").split("/").filter(Boolean).length <= CHIP_DEPTH;
}
