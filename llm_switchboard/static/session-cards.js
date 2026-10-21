import { hostBadge, hostLock, hostPanel } from "./hosts.js";
import { deviceClass, deviceTitle } from "./dirs.js";
import { age, esc, size } from "./format.js";

// Labels mirror supervisor.State in supervisor.py.
const STATE = {
  running: ["Remote Control on", "b-ok"],
  starting: ["Starting", "b-info"],
  attention: ["Needs attention", "b-warn"],
  elsewhere: ["Waiting: open elsewhere", "b-idle"],
  offline: ["Waiting for network", "b-idle"],
  backoff: ["Exited, relaunching", "b-warn"],
  missing: ["Transcript missing", "b-bad"],
  error: ["Error", "b-bad"],
};

const isCodex = (session) => session.tool === "codex";

export function sessionBlockedReason(session) {
  return hostLock(session) || (session.open_elsewhere.length
    ? `Open in another claude process (pid ${session.open_elsewhere[0].pid}); close it to rename, move or delete.` : "");
}

function badges(session) {
  const out = session.host ? [hostBadge(session)] : [];
  const status = session.status;
  if (session.enabled && status) {
    const [label, cls] = STATE[status.state] || [status.state, "b-idle"];
    out.push(`<span class="badge ${cls}">${esc(label)}</span>`);
    if (status.busy) out.push('<span class="badge b-info">Working</span>');
    if (status.rss_kb) out.push(`<span class="badge b-idle">${Math.round(status.rss_kb / 1024)} MB</span>`);
  } else if (session.enabled) {
    out.push('<span class="badge b-info">Queued</span>');
  }
  if (session.new) out.push('<span class="badge b-info">New</span>');
  if (isCodex(session) && session.codex_state === "active") out.push('<span class="badge b-info">Working</span>');
  if (session.open_elsewhere.length && !(session.enabled && status && status.state === "elsewhere")) {
    out.push('<span class="badge b-idle">Open in a terminal</span>');
  }
  return out.length ? `<div class="badges">${out.join("")}</div>` : "";
}

export function createSessionCards(selection, expanded, screens, peeks, directoryOf, menuState) {
  const copyButton = (text) => `<button class="btn" data-act="copy" data-text="${esc(text)}">Copy</button>`;

  function messagesBlock(id) {
    const messages = peeks.get(id);
    if (messages === undefined) return "";
    if (typeof messages === "string") return `<div class="hint">${esc(messages)}</div>`;
    if (!messages.length) return '<div class="hint">No messages yet.</div>';
    return `<div class="msgs">${messages.map((message) => `<div class="msg ${message.role}">${esc(message.text)}</div>`).join("")}</div>`;
  }

  function field(session, key, label, value, button, extra = "") {
    return `<div>
      <label for="${key}-${session.id}">${label}</label>
      <div class="line">
        <input type="text" id="${key}-${session.id}" value="${esc(value)}" maxlength="400" ${extra}>
        ${button}
      </div>
    </div>`;
  }

  function moveSplit(session, lock) {
    const menu = menuState();
    const open = menu?.id === session.id;
    return `<div class="split">
      <button class="btn" data-act="move" data-id="${session.id}" ${lock}>Move</button>
      <button class="btn caret" data-act="menu" data-id="${session.id}" aria-label="More folder actions" aria-haspopup="menu" aria-expanded="${open}">▾</button>
      ${open ? `<div class="menu" role="menu">
        <button type="button" role="menuitem" data-act="fork" data-id="${session.id}">Fork…<small>Copy the conversation into a new session; the original stays as it is</small></button>
      </div>` : ""}
    </div>`;
  }

  function codexPanel(session) {
    return `<div class="panel">
    <div class="actions">
      <button class="btn" data-act="peek" data-id="${session.id}">${peeks.has(session.id) ? "Refresh messages" : "Recent messages"}</button>
    </div>
    ${messagesBlock(session.id)}
    ${field(session, "title", "Title", session.title, `<button class="btn" data-act="rename" data-id="${session.id}">Rename</button>`)}
    ${field(session, "cwd", "Working folder (Codex can't move a session; Fork copies it into a folder)", session.cwd,
            `<button class="btn" data-act="fork" data-id="${session.id}">Fork…</button>`, 'list="cwds" spellcheck="false"')}
    <div>
      <label>Resume in a shell on the Pi</label>
      <div class="cmdline"><div class="cmd">${esc(session.resume_cmd)}</div>${copyButton(session.resume_cmd)}</div>
    </div>
    <div class="hint"><code>${esc(session.id)}</code></div>
  </div>`;
  }

  function panel(session) {
    if (session.host) return hostPanel(session);
    if (isCodex(session)) return codexPanel(session);
    const status = session.status || {};
    const screen = screens.get(session.id);
    const reason = sessionBlockedReason(session);
    const lock = reason ? `disabled title="${esc(reason)}"` : "";
    const exit = status.last_exit ? `<div class="hint">Last exit: status ${esc(status.last_exit.status)} after ${Math.round(status.last_exit.ran_s)}s, ${age(status.last_exit.at)} · launches: ${status.launches}</div>` : "";
    const attach = `llm-switchboard attach ${session.short_id}`;
    const menu = menuState();
    return `<div class="panel">
    <div class="actions">
      ${session.remote_url ? `<a class="btn" href="${esc(session.remote_url)}" target="_blank" rel="noopener">Open in Claude ↗</a>` : ""}
      <button class="btn" data-act="peek" data-id="${session.id}">${peeks.has(session.id) ? "Refresh messages" : "Recent messages"}</button>
      ${session.enabled ? `<button class="btn" data-act="screen" data-id="${session.id}">${screen !== undefined ? "Refresh screen" : "Terminal screen"}</button>` : ""}
      ${session.enabled ? `<button class="btn" data-act="restart" data-id="${session.id}">Restart</button>` : ""}
    </div>
    ${reason ? `<div class="hint">${esc(reason)}</div>` : ""}
    ${messagesBlock(session.id)}
    ${screen !== undefined ? `<div class="screen"><pre>${esc(screen || "(nothing to show)")}</pre>
      <button type="button" class="screen-close" data-act="screen-close" data-id="${session.id}" aria-label="Close terminal screen" title="Close">×</button></div>` : ""}
    ${field(session, "title", "Title", session.title, `<button class="btn" data-act="rename" data-id="${session.id}" ${lock}>Rename</button>`)}
    ${field(session, "cwd", "Working folder (Move takes the transcript there and a kept session restarts there; ▾ Fork copies it instead)",
            menu?.id === session.id ? menu.cwd : session.cwd, moveSplit(session, lock), 'list="cwds" spellcheck="false"')}
    ${field(session, "name", "Remote Control name (blank = session title; applies on next launch)", session.rc_name,
            `<button class="btn" data-act="name" data-id="${session.id}">Save</button>`, `placeholder="${esc(status.rc_name || session.title)}"`)}
    ${exit}
    <div>
      <label>Resume in a shell on the Pi</label>
      <div class="cmdline"><div class="cmd">${esc(session.resume_cmd)}</div>${copyButton(session.resume_cmd)}</div>
    </div>
    ${session.enabled ? `<div>
      <label>Attach to its tmux window (detach: Ctrl-b d)</label>
      <div class="cmdline"><div class="cmd">${esc(attach)}</div>${copyButton(attach)}</div>
    </div>` : ""}
    <div class="hint"><code>${esc(session.id)}</code></div>
  </div>`;
  }

  return {
    render(session) {
      const blockedReason = sessionBlockedReason(session);
      const status = session.status;
      const detail = session.enabled && status && status.detail && status.state !== "running" ? `<div class="detail">${esc(status.detail)}</div>` : "";
      const cls = ["card", session.host && !session.host_online && "host-offline", session.enabled && "enabled", selection.has(session.id) && "selected", selection.cursor === session.id && "cursor"].filter(Boolean).join(" ");
      return `<div class="${cls}" data-card="${session.id}">
    <div class="row">
      <button type="button" class="check" data-act="select" data-id="${session.id}" aria-label="Select" aria-pressed="${selection.has(session.id)}"></button>
      ${session.host ? '<span class="tool-tag">Mac</span>' : isCodex(session) ? '<span class="tool-tag" title="A Codex session">Codex</span>' : `<label class="switch" title="${session.enabled ? "Stop keeping this running" : "Keep this running with Remote Control"}">
        <input type="checkbox" data-act="toggle" data-id="${session.id}" ${session.enabled ? "checked" : ""} aria-label="Keep running">
        <span></span>
      </label>`}
      <div class="main">
        <div class="title">${esc(session.title)}</div>
        <div class="meta"><button type="button" class="dirlink ${deviceClass(session.host)}" data-dir="${esc(directoryOf(session))}" title="${esc(deviceTitle(session.host))}; show only this folder">${esc(session.project || "(unknown folder)")}</button><span>${age(session.mtime)}</span><code>${esc(session.short_id)}</code><span>${size(session.size)}</span></div>
        ${badges(session)}${detail}
      </div>
      <div class="side">
        <button type="button" class="more" data-act="expand" data-id="${session.id}" aria-label="Details">${expanded.has(session.id) ? "▴" : "▾"}</button>
        <button type="button" class="x" data-act="delete" data-id="${session.id}" aria-label="Delete" title="${esc(blockedReason || "Delete")}" ${blockedReason ? "disabled" : ""}>×</button>
      </div>
    </div>
    ${expanded.has(session.id) ? panel(session) : ""}
  </div>`;
    },
  };
}
