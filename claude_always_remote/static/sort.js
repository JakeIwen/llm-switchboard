// Orders for the session list. The server sends rows kept-first-then-recent;
// the page re-sorts on the client so the choice costs nothing and survives
// polling. Ties fall back to the server order, which is stable.

const recent = (a, b) => (b.mtime || 0) - (a.mtime || 0);
const byText = (f) => (a, b) => f(a).localeCompare(f(b), undefined, { sensitivity: "base", numeric: true });

export const SORTS = [
  { key: "kept", label: "Kept first", compare: (a, b) => Number(b.enabled) - Number(a.enabled) || recent(a, b) },
  { key: "recent", label: "Recent activity", compare: recent },
  { key: "oldest", label: "Oldest activity", compare: (a, b) => recent(b, a) },
  { key: "name", label: "Name A–Z", compare: byText((s) => s.title) },
  { key: "folder", label: "Folder, then recent", compare: (a, b) => byText((s) => s.project)(a, b) || recent(a, b) },
  { key: "size", label: "Largest first", compare: (a, b) => (b.size || 0) - (a.size || 0) || recent(a, b) },
];
export const DEFAULT_SORT = SORTS[0].key;

export function sortRows(rows, key) {
  const sort = SORTS.find((s) => s.key === key) || SORTS[0];
  return rows.map((row, i) => [row, i]).sort(([a, i], [b, j]) => sort.compare(a, b) || i - j).map(([row]) => row);
}
