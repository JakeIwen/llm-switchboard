// Toasts, dialog bodies and the clipboard.

const $ = (id) => document.getElementById(id);

let toastTimer = null;
// action: a function (shown as "Undo"), or { label, run } / { label, href }.
export function toast(msg, action, ms) {
  if (typeof action === "function") action = { label: "Undo", run: action };
  $("toast-msg").textContent = msg;
  $("toast-action").hidden = !action;
  if (action) {
    $("toast-action").textContent = action.label;
    $("toast-action").onclick = () => {
      $("toast").hidden = true;
      action.href ? window.open(action.href, "_blank", "noopener") : action.run();
    };
  }
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, ms || (action ? 10000 : 5000));
}

// A dialog body listing session titles (at most 8) and some notes.
export function summaryBody(sessions, notes) {
  const box = document.createElement("div");
  const ul = document.createElement("ul");
  for (const s of sessions.slice(0, 8)) ul.append(Object.assign(document.createElement("li"), { textContent: s.title }));
  if (sessions.length > 8) ul.append(Object.assign(document.createElement("li"), { textContent: `…and ${sessions.length - 8} more` }));
  if (sessions.length > 1) box.append(ul);
  for (const n of notes.filter(Boolean)) box.append(Object.assign(document.createElement("p"), { textContent: n }));
  return box;
}

export async function copyText(text) {
  // navigator.clipboard needs a secure (https) page; plain http on the LAN falls back to execCommand.
  try { await navigator.clipboard.writeText(text); return true; } catch {}
  const ta = document.createElement("textarea");
  ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch {}
  ta.remove();
  return ok;
}
