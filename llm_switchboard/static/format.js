// Text formatting shared by the page's modules.

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const nowS = () => Date.now() / 1000;
export const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function age(t) {
  if (!t) return "";
  const s = nowS() - t;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
export const size = (b) => b == null ? "" : b < 1048576 ? (b / 1024).toFixed(0) + " KB" : (b / 1048576).toFixed(1) + " MB";
