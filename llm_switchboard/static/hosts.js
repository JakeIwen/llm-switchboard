// Mac metadata is pushed over SSH. This page never contacts the Mac.
import { esc } from "./format.js";

const seenTime = (time) => new Date(time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
export const hostStatus = (host) => host.online ? "online" : `offline, last seen ${seenTime(host.last_seen)}`;
export const hostSummary = (hosts = []) => hosts.map((h) => `${h.host}: ${hostStatus(h)}`).join(" · ");
export const hostBadge = (s) => `<span class="badge ${s.host_online ? "b-info" : "b-idle"}">${esc(s.host)} · ${esc(hostStatus({ online: s.host_online, last_seen: s.last_seen }))}</span>`;
export const hostLock = (s) => s.host ? "Mac sessions are list-only in this version." : "";

export function hostPanel(s) {
  return `<div class="panel">
    <div><label>Title</label>${esc(s.title)}</div>
    <div><label>Working folder on the Mac</label><div class="cmd">${esc(s.cwd)}</div></div>
    <div><label>Resume in a shell on the Mac</label>
      <div class="cmdline"><div class="cmd">${esc(s.resume_cmd)}</div>
      <button class="btn" data-act="copy" data-text="${esc(s.resume_cmd)}">Copy</button></div></div>
    <div class="hint">Messages and terminal screens stay on the Mac. This command uses plain ${s.tool === "codex" ? "codex" : "claude"}, not a shell alias.</div>
  </div>`;
}
