// Mac metadata is pushed over SSH. This page never contacts the Mac.
import { esc } from "./format.js";
import { api } from "./api.js";
import { toast } from "./feedback.js";
import { confirmDialog, formDialog } from "./dialog.js";

// Mirrors host_actions_protocol.ActionState; sent remains visibly queued until a result arrives.
const ACTION_STATE = { queued: "queued", sent: "queued (delivered; awaiting result)", done: "done", failed: "failed", expired: "expired" };
const seenTime = (time) => new Date(time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
export const hostStatus = (host) => host.online ? "online" : `offline, last seen ${seenTime(host.last_seen)}`;
export const hostSummary = (hosts = []) => hosts.map((h) => `${h.host}: ${hostStatus(h)}`).join(" · ");
export const hostBadge = (s) => `<span class="badge ${s.host_online ? "b-info" : "b-idle"}">${esc(s.host)} · ${esc(hostStatus({ online: s.host_online, last_seen: s.last_seen }))} · ${s.tool === "codex" ? "Codex" : "Claude"}</span>`;
export function hostLock(s) {
  if (!s.host) return "";
  if (!s.host_online) return "Mac is offline; wait for its next push.";
  if (!s.mutable) return "Mac Codex is list-only until its app-server daemon is available.";
  if (s.state !== "closed") return "Open or working on the Mac; close it before changing it.";
  return "";
}
export function actionPath(id, action) {
  const colon = id.indexOf(":");
  return colon < 0 ? `/api/sessions/${id}/${action}`
    : `/api/hosts/${id.slice(0, colon)}/sessions/${id.slice(colon + 1)}/${action}`;
}
export const hasRemote = (ids) => ids.some((id) => id.includes(":"));
export const actionSummary = (actions = []) => actions.slice(-20).reverse().map((a) =>
  `<div>${esc(a.host)} · ${esc(a.title)} · ${esc(a.action)}: <strong>${esc(ACTION_STATE[a.state] || a.state)}</strong>${a.message ? " — " + esc(a.message) : ""}</div>`).join("");
export const trashHost = (t) => t.host ? `<span>${esc(t.host)} · ${esc(hostStatus({ online: t.host_online, last_seen: t.last_seen }))}</span>` : "";
export const restoreLock = (t) => hostLock(t) ? `disabled title="${esc(hostLock(t))}"` : "";

export function hostPanel(s) {
  const locked = hostLock(s), attr = locked ? `disabled title="${esc(locked)}"` : "";
  const button = (action, label) => `<button class="btn" data-act="host-${action}" data-id="${s.id}" ${attr}>${label}</button>`;
  return `<div class="panel">
    <div><label for="title-${s.id}">Title</label><div class="line">
      <input type="text" id="title-${s.id}" value="${esc(s.title)}" maxlength="120">${button("rename", "Rename")}</div></div>
    <div><label for="cwd-${s.id}">Working folder on the Mac</label><div class="line">
      <input type="text" id="cwd-${s.id}" value="${esc(s.cwd)}" maxlength="4096" spellcheck="false">
      ${s.tool !== "codex" ? button("move", "Move") : ""}</div></div>
    <div class="actions">${button("fork", "Fork on the Mac…")}${s.tool !== "codex" ? button("fork-pi", "Fork onto the Pi…") : ""}${button("delete", "Delete")}</div>
    <div><label>Resume in a shell on the Mac</label>
      <div class="cmdline"><div class="cmd">${esc(s.resume_cmd)}</div>
      <button class="btn" data-act="copy" data-text="${esc(s.resume_cmd)}">Copy</button></div></div>
    <div class="hint">${esc(locked || "Actions expire after 60 seconds. Forks never start running.")} Messages and terminal screens stay on the Mac, except the one transcript you explicitly fork onto the Pi. Resume uses plain ${s.tool === "codex" ? "codex" : "claude"}.</div>
  </div>`;
}

export async function hostAction(el, session, refresh) {
  const kind = el.dataset.act.slice(5);
  if (hostLock(session)) return toast(hostLock(session));
  let body = {};
  const value = (key) => document.getElementById(`${key}-${session.id}`).value.trim();
  if (kind === "rename") body.title = value("title");
  if (kind === "move") body.cwd = value("cwd");
  if (kind === "fork" || kind === "fork-pi") {
    const pi = kind === "fork-pi";
    const fields = [{ name: "title", label: "Name for the copy", required: true, maxlength: 120, autofocus: true,
      value: `${session.title.slice(0, 100)} (fork)` }];
    if (!pi) fields.push({ name: "cwd", label: "Existing folder on the Mac", value: value("cwd"), required: true });
    body = await formDialog({ title: pi ? "Fork onto the Pi" : "Fork on the Mac", fields, confirm: "Queue fork",
      body: pi ? "Uploads this one transcript to the Pi. /Users/jacobr/<folder> maps to /home/pi/<folder>, which must exist. The Mac posts a notification and logs the upload (10 per hour; 16 MiB maximum). The copy does not start running."
        : "Copies the conversation on the Mac. No process starts, and the copy is not kept running." });
    if (!body) return;
  }
  if (kind === "delete" && !await confirmDialog({ title: `Delete “${session.title}” on the Mac?`,
    body: "Queues a move to the Mac's trash for 30 days. No transcript is uploaded.", confirm: "Queue delete", danger: true })) return;
  try {
    await api(actionPath(session.id, kind), body);
    toast(`Queued ${kind} on ${session.host}; watch the action status for its result.`);
  } catch (e) { toast(`Couldn't queue ${kind}: ${e.message}`); }
  el.blur();
  refresh();
}
