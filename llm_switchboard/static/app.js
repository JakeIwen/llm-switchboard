import { hostBadge, hostPanel, hostSummary, hostLock, hostAction, actionPath, actionSummary, hasRemote, trashHost, restoreLock } from "./hosts.js";
import { compileQuery } from "./search.js";
import { deviceClass, deviceTitle, dirKey, hasChip } from "./dirs.js";
import { initFullText } from "./fulltext.js";
import { DEFAULT_SORT, SORTS, sortRows } from "./sort.js";
import { Selection } from "./selection.js";
import { confirmDialog, formDialog, promptDialog } from "./dialog.js";
import { api } from "./api.js";
import { copyText, summaryBody, toast } from "./feedback.js";
import { age, esc, nowS, plural, size } from "./format.js";

// Labels for the supervisor's State enum (supervisor.py).
const STATE = {
  running:   ["Remote Control on", "b-ok"],
  starting:  ["Starting", "b-info"],
  attention: ["Needs attention", "b-warn"],
  elsewhere: ["Waiting: open elsewhere", "b-idle"],
  offline:   ["Waiting for network", "b-idle"],
  backoff:   ["Exited, relaunching", "b-warn"],
  missing:   ["Transcript missing", "b-bad"],
  error:     ["Error", "b-bad"],
};
const UNKNOWN_DIR = "(unknown folder)";
// Supervisor states that mean a new session didn't come up by itself.
const TROUBLE = new Set(["attention", "backoff", "error", "missing"]);
const TABS = { "f-all": "all", "f-on": "on", "f-open": "open" };
const TOOLS = { "t-all": "all", "t-claude": "claude", "t-codex": "codex" };
const TOOL_NAME = { claude: "Claude Code", codex: "Codex" };
const isCodex = (s) => s.tool === "codex";

const $ = (id) => document.getElementById(id);
const dirOf = (s) => dirKey(s.host, s.project || UNKNOWN_DIR);

// ---- state -----------------------------------------------------------------

let data = null, trash = [], tab = "all", tool = "all", dir = null, query = compileQuery("");
let fulltext = null;
let order = [];            // ids of the rows on screen, in order
let bulkProgress = "";     // non-empty while a bulk action runs
const expanded = new Set(), screens = new Map(), peeks = new Map();
const sel = new Selection();
const watching = new Map();  // id -> title of sessions created here, until they're ready
// The open Move ▾ menu: { id, cwd }, keeping the folder typed before it opened
// (opening it redraws the panel's fields).
let menu = null;
// The chosen folder is a per-browser convenience; storage can be unavailable.
let sort = DEFAULT_SORT;
try { dir = localStorage.getItem("switchboard.dir"); tool = localStorage.getItem("switchboard.tool") || "all"; sort = localStorage.getItem("switchboard.sort") || sort; } catch {}

const byId = (id) => data.sessions.find((s) => s.id === id);

// Replacing a node mid-click (between mousedown and mouseup) swallows the
// click, so the list is only rewritten when its markup changed, and polling
// waits while a pointer is down.
let pointerDown = false;
document.addEventListener("pointerdown", () => { pointerDown = true; }, true);
document.addEventListener("pointerup", () => { pointerDown = false; setTimeout(render); }, true);
document.addEventListener("pointercancel", () => { pointerDown = false; }, true);
const lastHTML = new WeakMap();
function setHTML(el, html) {
  if (lastHTML.get(el) === html) return;
  el.innerHTML = html;
  lastHTML.set(el, html);
}

async function refresh() {
  try {
    [data, { trash }] = await Promise.all([api("/api/sessions"), api("/api/trash")]);
    if (!pointerDown) render();
    fulltext?.poll();
    checkWatched();
  } catch (e) {
    setHTML($("list"), `<div class="err">Can't reach the service: ${esc(e.message)}</div>`);
  }
}

// Every filter except the folder one, so folder counts follow the search and tab.
function matches(s, now) {
  // The tool switch is hidden (and ignored) while Codex support is off.
  if (tool !== "all" && (data.codex?.enabled || data.sessions.some((r) => r.tool === "codex")) && s.tool !== tool) return false;
  if (tab === "on" && !s.enabled) return false;
  if (tab === "open" && !(s.enabled || s.open_elsewhere.length)) return false;
  return query.test(s, now);
}

function setDir(d) {
  dir = d;
  try { d === null ? localStorage.removeItem("switchboard.dir") : localStorage.setItem("switchboard.dir", d); } catch {}
  render();
}

// ---- rendering -------------------------------------------------------------

function renderDirs(now) {
  // Folders ordered by their most recent session; deep or outside-home ones get no chip (dirs.js).
  const dirs = new Map();
  for (const s of data.sessions) {
    const d = dirs.get(dirOf(s)) || { latest: 0, n: 0, host: s.host || "", project: s.project || UNKNOWN_DIR };
    d.latest = Math.max(d.latest, s.mtime || 0);
    if (matches(s, now)) d.n++;
    dirs.set(dirOf(s), d);
  }
  if (dir !== null && !dirs.has(dir)) dirs.set(dir, { latest: 0, n: 0, host: null, project: dir });
  const total = data.sessions.filter((s) => matches(s, now)).length;
  // host is null for All folders, and for a selected folder no row has any more: no machine, no tint.
  const chip = (d, label, n, host = null) => `<button type="button" class="chip ${dir === d ? "on" : ""} ${n ? "" : "zero"} ${host === null ? "" : deviceClass(host)}" ${host === null ? "" : `title="${esc(deviceTitle(host))}"`} data-dir="${esc(d ?? "")}" ${d === null ? "data-all" : ""}>${esc(label)}<span class="n">${n}</span></button>`;
  // The selected folder keeps its chip even when it has none, so it can be turned off.
  const sorted = [...dirs].filter(([d, v]) => hasChip(v.project) || d === dir).sort((a, b) => b[1].latest - a[1].latest);
  setHTML($("dirs"), chip(null, "All folders", total) + sorted.map(([d, v]) => chip(d, v.project, v.n, v.host)).join(""));
}

function badges(s) {
  const out = s.host ? [hostBadge(s)] : [];
  const st = s.status;
  if (s.enabled && st) {
    const [label, cls] = STATE[st.state] || [st.state, "b-idle"];
    out.push(`<span class="badge ${cls}">${esc(label)}</span>`);
    if (st.busy) out.push(`<span class="badge b-info">Working</span>`);
    if (st.rss_kb) out.push(`<span class="badge b-idle">${Math.round(st.rss_kb / 1024)} MB</span>`);
  } else if (s.enabled) {
    out.push(`<span class="badge b-info">Queued</span>`);
  }
  if (s.new) out.push(`<span class="badge b-info">New</span>`);
  if (isCodex(s) && s.codex_state === "active") out.push(`<span class="badge b-info">Working</span>`);
  if (s.open_elsewhere.length && !(s.enabled && st && st.state === "elsewhere")) {
    out.push(`<span class="badge b-idle">Open in a terminal</span>`);
  }
  return out.length ? `<div class="badges">${out.join("")}</div>` : "";
}

const copyButton = (text) => `<button class="btn" data-act="copy" data-text="${esc(text)}">Copy</button>`;
const elsewhereNote = (s) => hostLock(s) || (s.open_elsewhere.length
  ? `Open in another claude process (pid ${s.open_elsewhere[0].pid}); close it to rename, move or delete.` : "");
const lockAttr = (s) => elsewhereNote(s) ? `disabled title="${esc(elsewhereNote(s))}"` : "";

function messagesBlock(id) {
  const m = peeks.get(id);
  if (m === undefined) return "";
  if (typeof m === "string") return `<div class="hint">${esc(m)}</div>`;
  if (!m.length) return `<div class="hint">No messages yet.</div>`;
  return `<div class="msgs">${m.map((x) => `<div class="msg ${x.role}">${esc(x.text)}</div>`).join("")}</div>`;
}

function field(s, key, label, value, button, extra = "") {
  return `<div>
      <label for="${key}-${s.id}">${label}</label>
      <div class="line">
        <input type="text" id="${key}-${s.id}" value="${esc(value)}" maxlength="400" ${extra}>
        ${button}
      </div>
    </div>`;
}

function moveSplit(s, lock) {
  const open = menu?.id === s.id;
  return `<div class="split">
      <button class="btn" data-act="move" data-id="${s.id}" ${lock}>Move</button>
      <button class="btn caret" data-act="menu" data-id="${s.id}" aria-label="More folder actions" aria-haspopup="menu" aria-expanded="${open}">▾</button>
      ${open ? `<div class="menu" role="menu">
        <button type="button" role="menuitem" data-act="fork" data-id="${s.id}">Fork…<small>Copy the conversation into a new session; the original stays as it is</small></button>
      </div>` : ""}
    </div>`;
}

// Codex sessions have no keep-running, screen, Remote Control name or move.
function codexPanel(s) {
  return `<div class="panel">
    <div class="actions">
      <button class="btn" data-act="peek" data-id="${s.id}">${peeks.has(s.id) ? "Refresh messages" : "Recent messages"}</button>
    </div>
    ${messagesBlock(s.id)}
    ${field(s, "title", "Title", s.title, `<button class="btn" data-act="rename" data-id="${s.id}">Rename</button>`)}
    ${field(s, "cwd", "Working folder (Codex can't move a session; Fork copies it into a folder)", s.cwd,
            `<button class="btn" data-act="fork" data-id="${s.id}">Fork…</button>`, `list="cwds" spellcheck="false"`)}
    <div>
      <label>Resume in a shell on the Pi</label>
      <div class="cmdline"><div class="cmd">${esc(s.resume_cmd)}</div>${copyButton(s.resume_cmd)}</div>
    </div>
    <div class="hint"><code>${esc(s.id)}</code></div>
  </div>`;
}

function panel(s) {
  if (s.host) return hostPanel(s);
  if (isCodex(s)) return codexPanel(s);
  const st = s.status || {};
  const screen = screens.get(s.id);
  const lock = lockAttr(s);
  const exit = st.last_exit ? `<div class="hint">Last exit: status ${esc(st.last_exit.status)} after ${Math.round(st.last_exit.ran_s)}s, ${age(st.last_exit.at)} · launches: ${st.launches}</div>` : "";
  const attach = `llm-switchboard attach ${s.short_id}`;
  return `<div class="panel">
    <div class="actions">
      ${s.remote_url ? `<a class="btn" href="${esc(s.remote_url)}" target="_blank" rel="noopener">Open in Claude ↗</a>` : ""}
      <button class="btn" data-act="peek" data-id="${s.id}">${peeks.has(s.id) ? "Refresh messages" : "Recent messages"}</button>
      ${s.enabled ? `<button class="btn" data-act="screen" data-id="${s.id}">${screen !== undefined ? "Refresh screen" : "Terminal screen"}</button>` : ""}
      ${s.enabled ? `<button class="btn" data-act="restart" data-id="${s.id}">Restart</button>` : ""}
    </div>
    ${elsewhereNote(s) ? `<div class="hint">${esc(elsewhereNote(s))}</div>` : ""}
    ${messagesBlock(s.id)}
    ${screen !== undefined ? `<div class="screen"><pre>${esc(screen || "(nothing to show)")}</pre>
      <button type="button" class="screen-close" data-act="screen-close" data-id="${s.id}" aria-label="Close terminal screen" title="Close">×</button></div>` : ""}
    ${field(s, "title", "Title", s.title, `<button class="btn" data-act="rename" data-id="${s.id}" ${lock}>Rename</button>`)}
    ${field(s, "cwd", "Working folder (Move takes the transcript there and a kept session restarts there; ▾ Fork copies it instead)",
            menu?.id === s.id ? menu.cwd : s.cwd, moveSplit(s, lock), `list="cwds" spellcheck="false"`)}
    ${field(s, "name", "Remote Control name (blank = session title; applies on next launch)", s.rc_name,
            `<button class="btn" data-act="name" data-id="${s.id}">Save</button>`, `placeholder="${esc(st.rc_name || s.title)}"`)}
    ${exit}
    <div>
      <label>Resume in a shell on the Pi</label>
      <div class="cmdline"><div class="cmd">${esc(s.resume_cmd)}</div>${copyButton(s.resume_cmd)}</div>
    </div>
    ${s.enabled ? `<div>
      <label>Attach to its tmux window (detach: Ctrl-b d)</label>
      <div class="cmdline"><div class="cmd">${esc(attach)}</div>${copyButton(attach)}</div>
    </div>` : ""}
    <div class="hint"><code>${esc(s.id)}</code></div>
  </div>`;
}

function card(s) {
  const st = s.status;
  const detail = s.enabled && st && st.detail && st.state !== "running" ? `<div class="detail">${esc(st.detail)}</div>` : "";
  const cls = ["card", s.host && !s.host_online && "host-offline", s.enabled && "enabled", sel.has(s.id) && "selected", sel.cursor === s.id && "cursor"].filter(Boolean).join(" ");
  return `<div class="${cls}" data-card="${s.id}">
    <div class="row">
      <button type="button" class="check" data-act="select" data-id="${s.id}" aria-label="Select" aria-pressed="${sel.has(s.id)}"></button>
      ${s.host ? `<span class="tool-tag">Mac</span>` : isCodex(s) ? `<span class="tool-tag" title="A Codex session">Codex</span>` : `<label class="switch" title="${s.enabled ? "Stop keeping this running" : "Keep this running with Remote Control"}">
        <input type="checkbox" data-act="toggle" data-id="${s.id}" ${s.enabled ? "checked" : ""} aria-label="Keep running">
        <span></span>
      </label>`}
      <div class="main">
        <div class="title">${esc(s.title)}</div>
        <div class="meta"><button type="button" class="dirlink ${deviceClass(s.host)}" data-dir="${esc(dirOf(s))}" title="${esc(deviceTitle(s.host))}; show only this folder">${esc(s.project || UNKNOWN_DIR)}</button><span>${age(s.mtime)}</span><code>${esc(s.short_id)}</code><span>${size(s.size)}</span></div>
        ${badges(s)}${detail}
      </div>
      <div class="side">
        <button type="button" class="more" data-act="expand" data-id="${s.id}" aria-label="Details">${expanded.has(s.id) ? "▴" : "▾"}</button>
        <button type="button" class="x" data-act="delete" data-id="${s.id}" aria-label="Delete" title="${esc(elsewhereNote(s) || "Delete")}" ${elsewhereNote(s) ? "disabled" : ""}>×</button>
      </div>
    </div>
    ${expanded.has(s.id) ? panel(s) : ""}
  </div>`;
}

function renderTrash() {
  $("trash").hidden = !trash.length;
  $("trash-empty").disabled = trash.every((t) => t.host);
  $("trash-sum").textContent = `Recently deleted (${trash.length}) · kept 30 days`;
  setHTML($("trash-list"), trash.map((t) => `<div class="card">
    <div class="main"><div class="title">${esc(t.title)}</div>
      <div class="meta"><span class="${deviceClass(t.host)}" title="${esc(deviceTitle(t.host))}">${esc(t.project)}</span><span>deleted ${age(t.deleted_at)}</span><code>${esc(t.short_id)}</code>${t.tool === "codex" ? "<span>Codex</span>" : ""}${trashHost(t)}</div></div>
    <button class="btn" data-restore="${t.id}" ${restoreLock(t)}>Restore</button></div>`).join(""));
}

function renderBulkbar() {
  const n = sel.size;
  $("bulkbar").hidden = !n && !bulkProgress;
  document.body.classList.toggle("bulk", !$("bulkbar").hidden);
  $("bulk-count").textContent = bulkProgress || `${n} selected`;
  for (const b of $("bulkbar").querySelectorAll("button")) b.disabled = Boolean(bulkProgress);
}

function renderCwds() {
  const cwds = [...new Set(data.sessions.map((s) => s.cwd).filter(Boolean))].sort();
  setHTML($("cwds"), cwds.map((c) => `<option value="${esc(c)}"></option>`).join(""));
}

function render() {
  if (!data || fulltext?.active) return;
  // Don't redraw under someone typing in a session's field.
  if (document.activeElement && document.activeElement.matches("#list input[type=text]")) return;
  const now = nowS();
  const rows = sortRows(data.sessions.filter((s) => matches(s, now) && (dir === null || dirOf(s) === dir)), sort);
  order = rows.map((s) => s.id);
  sel.prune(order);
  const on = data.sessions.filter((s) => s.enabled);
  const rc = on.filter((s) => s.status && s.status.state === "running").length;
  $("summary").textContent = `${data.host} · ${on.length} kept running · ${rc} with Remote Control${data.hosts?.length ? " · " + hostSummary(data.hosts) : ""}`;
  $("tools").hidden = !data.codex?.enabled && !data.sessions.some((s) => s.tool === "codex");
  $("qerr").textContent = [...query.errors, data.codex?.error && `Codex sessions unavailable: ${data.codex.error}`].filter(Boolean).join(" · ");
  setHTML($("host-actions"), actionSummary(data.host_actions, data.now));
  renderDirs(now);
  setHTML($("list"), rows.length ? rows.map(card).join("") : `<div class="empty">No sessions match.</div>`);
  renderTrash();
  renderBulkbar();
  renderCwds();
}

// ---- actions ---------------------------------------------------------------

async function act(id, action, body = {}) {
  try {
    await api(actionPath(id, action), body);
    return true;
  } catch (e) {
    toast(`${action[0].toUpperCase() + action.slice(1)} failed: ${e.message}`);
    return false;
  } finally {
    setTimeout(refresh, 600);
  }
}

// Runs fn on each id in turn, showing progress in the bulk bar. Returns the ids that worked.
async function runBulk(verb, ids, fn) {
  const done = [], failed = [];
  for (const [i, id] of ids.entries()) {
    bulkProgress = ids.length > 1 ? `${verb} ${i + 1} of ${ids.length}…` : `${verb}…`;
    renderBulkbar();
    try { await fn(id); done.push(id); }
    catch (e) { failed.push(`${byId(id)?.title || id}: ${e.message}`); }
  }
  bulkProgress = "";
  await refresh();
  if (failed.length) {
    await confirmDialog({ title: `${plural(failed.length, "session")} failed`, body: failed.join("\n"), confirm: "OK" });
  }
  return done;
}

// Sessions open in another claude process can't be renamed, moved or deleted.
function splitBlocked(ids) {
  const list = ids.map(byId).filter(Boolean);
  return { targets: list.filter((s) => !elsewhereNote(s)), blocked: list.filter((s) => elsewhereNote(s)) };
}

async function deleteSessions(ids) {
  const { targets, blocked } = splitBlocked(ids);
  if (!targets.length) return toast("Selected sessions are open, offline or list-only; close them or wait for the Mac.");
  const kept = targets.filter((s) => s.enabled).length;
  const busy = targets.filter((s) => s.status?.busy).length;
  const one = targets.length === 1;
  const ok = await confirmDialog({
    title: one ? `Delete “${targets[0].title}”?` : `Delete ${targets.length} sessions?`,
    body: summaryBody(targets, [
      `${one ? "It goes" : "They go"} to Recently deleted for 30 days.`,
      kept && (one ? "It's kept running and will be stopped." : `${kept} kept running will be stopped.`),
      busy && (one ? "It's working on a turn right now." : `${busy} are working on a turn right now.`),
      blocked.length && `${plural(blocked.length, "session")} open, offline or list-only will be skipped.`,
    ]),
    confirm: "Delete",
    danger: true,
  });
  if (!ok) return;
  const done = await runBulk("Deleting", targets.map((s) => s.id), (id) => api(actionPath(id, "delete"), {}));
  for (const id of done) { expanded.delete(id); sel.ids.delete(id); }
  render();
  if (done.length) {
    const what = done.length === 1 ? `“${targets.find((s) => s.id === done[0]).title}”` : plural(done.length, "session");
    toast(hasRemote(done) ? `Local deletes done; Mac deletes queued. Watch their action status.` : `Deleted ${what}.`, hasRemote(done) ? null : () => restoreMany(done));
  }
}

async function newSession() {
  let folders;
  try { ({ folders } = await api("/api/folders")); }
  catch (e) { return toast(`Couldn't load folders: ${e.message}`); }
  const codexOn = Boolean(data.codex?.enabled && !data.codex.error);
  const usable = folders.filter((f) => !f.blocked || (codexOn && !f.codex_blocked));
  const blocked = folders.filter((f) => f.blocked);
  const notes = document.createElement("div");
  const note = (text) => notes.append(Object.assign(document.createElement("p"), { textContent: text }));
  note(`${codexOn ? "Claude Code: " : ""}starts through claudepd with Remote Control on, and is kept running.${data.notifications ? " You'll get a notification when it's ready." : ""}`);
  if (codexOn) note("Codex: starts in the Codex daemon and works on your first message right away, without asking for approvals (as codexp). Codex saves a session only once it has a message.");
  if (blocked.length) {
    note(codexOn ? "Not offered to Claude Code:" : "Not offered:");
    const ul = document.createElement("ul");
    for (const f of blocked) ul.append(Object.assign(document.createElement("li"), { textContent: `${f.project}: ${f.blocked}` }));
    notes.append(ul);
  }
  if (!usable.length) return confirmDialog({ title: "No folder is available", body: notes, confirm: "OK" });
  const fields = [
    { name: "cwd", label: "Folder", kind: "select",
      value: usable.find((f) => f.project === dir)?.cwd,
      options: usable.map((f) => ({ value: f.cwd, label: `${f.project}  (${plural(f.sessions, "session")})` })) },
    { name: "name", label: "Name", placeholder: "What is this session for?", required: true, autofocus: true, maxlength: 120 },
  ];
  if (codexOn) {
    fields.unshift({ name: "tool", label: "Tool", kind: "select", value: tool === "codex" ? "codex" : "claude",
                     options: Object.entries(TOOL_NAME).map(([value, label]) => ({ value, label })) });
    fields.push({ name: "prompt", label: "First message", placeholder: "What should Codex do first?", required: true,
                  maxlength: 20000, when: (v) => v.tool === "codex" });
  }
  const res = await formDialog({
    title: "New session",
    body: notes,
    confirm: "Create",
    fields,
    validate: (v) => {
      const why = folders.find((f) => f.cwd === v.cwd)?.[v.tool === "codex" ? "codex_blocked" : "blocked"];
      return why ? `${TOOL_NAME[v.tool || "claude"]} can't start here: ${why}` : nameTaken(v.cwd, v.name);
    },
  });
  if (!res) return;
  let created;
  try { created = await api("/api/sessions/new", { tool: res.tool || "claude", cwd: res.cwd, name: res.name, prompt: res.prompt }); }
  catch (e) { return toast(`Couldn't create it: ${e.message}`); }
  sel.moveCursor(created.id);
  if (res.tool === "codex") {
    toast(`Started “${created.title}” in Codex; it's working on your first message.`, null, 15000);
  } else {
    watching.set(created.id, created.title);
    toast(`Starting “${created.title}”…`, null, 30000);
  }
  refresh();
}

// Tell the page (as well as the phone, via the server) when a new session is up.
function checkWatched() {
  for (const [id, title] of watching) {
    const s = byId(id);
    if (!s || !s.enabled) { watching.delete(id); continue; }
    const state = s.status?.state;
    if (state === "running") {
      watching.delete(id);
      toast(`“${title}” is ready with Remote Control.`, s.remote_url ? { label: "Open in Claude", href: s.remote_url } : null, 20000);
    } else if (TROUBLE.has(state)) {
      watching.delete(id);
      toast(`“${title}” needs attention: ${s.status.detail}`, {
        label: "Details", run: () => { expanded.add(id); sel.moveCursor(id); render(); scrollToCursor(); },
      }, 20000);
    }
  }
}

// Names are unique per folder (the server enforces it; this explains it early).
const nameKey = (t) => t.trim().split(/\s+/).join(" ").toLowerCase();
function nameTaken(cwd, title) {
  const folder = cwd.trim().replace(/(.)\/+$/, "$1");
  const other = data.sessions.find((s) => !s.host && (s.cwd === folder || s.project === folder) && nameKey(s.title) === nameKey(title));
  return other ? `${other.project} already has a session named “${other.title}”.` : "";
}

function forkName(s) {
  for (let n = 1; ; n++) {
    const name = `${s.title.slice(0, 100)} (fork${n > 1 ? " " + n : ""})`;
    if (!nameTaken(s.cwd, name)) return name;
  }
}

async function forkSession(id, cwd) {
  const s = byId(id);
  menu = null;
  render();
  const fields = [
    { name: "title", label: "Name", required: true, autofocus: true, maxlength: 120, value: forkName(s) },
    { name: "cwd", label: "Folder", required: true, value: cwd || s.cwd, list: "cwds", placeholder: "/home/pi/dev/…" },
  ];
  if (!isCodex(s)) fields.push({ name: "keep", label: "Keep it running with Remote Control", kind: "checkbox", value: s.enabled });
  const res = await formDialog({
    title: `Fork “${s.title}”`,
    body: `Copies the conversation so far into a new ${TOOL_NAME[s.tool]} session. The original is left as it is, even if it's running.`,
    confirm: "Fork",
    fields,
    validate: (v) => nameTaken(v.cwd, v.title),
  });
  if (!res) return;
  let made;
  try { made = await api(`/api/sessions/${id}/fork`, { ...res, cwd: res.cwd.trim() }); }
  catch (e) { return toast(`Fork failed: ${e.message}`); }
  if (res.keep) watching.set(made.id, made.title);
  sel.moveCursor(made.id);
  await refresh();
  scrollToCursor();
  toast(`Forked as “${made.title}”${res.keep ? "; starting it…" : "."}`);
}

async function restoreMany(ids) {
  const done = await runBulk("Restoring", ids, (id) => api(actionPath(id, "restore"), {}));
  if (done.length) toast(hasRemote(done) ? "Mac restores queued; watch their action status." : `Restored ${plural(done.length, "session")}.`);
}

async function setKept(ids, on) {
  const skipped = ids.map(byId).filter((s) => s?.host).length;
  ids = ids.filter((id) => !byId(id)?.host);
  if (!ids.length) return toast(`${skipped} Mac sessions skipped: the Pi cannot keep them running.`);
  const targets = ids.map(byId).filter((s) => s && !isCodex(s) && s.enabled !== on);
  if (!targets.length) {
    if (skipped) return toast(`${skipped} Mac sessions skipped; no selected Pi Claude session needs this change.`);
    if (ids.map(byId).every((s) => s && isCodex(s))) return toast("Codex sessions have no keep-running; the Codex daemon holds them all.");
    return toast(on ? "Already kept running." : "None of these are kept running.");
  }
  const busy = targets.filter((s) => s.status?.busy).length;
  if (on && targets.length > 2 && !await confirmDialog({
    title: `Keep ${targets.length} sessions running?`,
    body: summaryBody(targets, ["Each is a claude process using roughly 300 MB."]), confirm: "Keep running" })) return;
  if (!on && busy && !await confirmDialog({
    title: `Stop ${plural(targets.length, "session")}?`,
    body: `${plural(busy, "session")} working on a turn right now.`, confirm: "Stop", danger: true })) return;
  const done = await runBulk(on ? "Enabling" : "Stopping", targets.map((s) => s.id),
                             (id) => api(`/api/sessions/${id}/${on ? "enable" : "disable"}`, {}));
  if (done.length) toast(`${on ? "Keeping" : "Stopped keeping"} ${plural(done.length, "session")}${on ? " running" : ""}.${skipped ? ` Skipped ${skipped} Mac sessions.` : ""}`);
}

async function moveSessions(ids, cwd) {
  const split = splitBlocked(ids);
  const targets = split.targets.filter((s) => !isCodex(s)), blocked = split.blocked;
  const codex = split.targets.length - targets.length;
  if (!targets.length) return toast(codex ? "Codex can't move a session; fork it into the folder instead." : "Open in another claude process; close it there first.");
  if (cwd === undefined) {
    const common = targets.every((s) => s.cwd === targets[0].cwd) ? targets[0].cwd : "";
    const one = targets.length === 1;
    const body = summaryBody(targets, [
      `${one ? "Its transcript moves" : "Their transcripts move"} under the new folder, so claudepd --resume works from there.`,
      targets.some((s) => s.enabled) && "Kept-running sessions restart in the new folder.",
      blocked.length && `${plural(blocked.length, "session")} open, offline or list-only will be skipped.`,
      codex && `${plural(codex, "Codex session")} will be skipped; Codex can't move one (fork it instead).`,
    ]);
    cwd = await promptDialog({
      title: one ? `Move “${targets[0].title}”` : `Move ${targets.length} sessions`,
      body, confirm: "Move", value: common, placeholder: "/home/pi/dev/…", list: "cwds",
    });
    if (cwd === null) return;
  }
  cwd = cwd.trim();
  if (!cwd) return;
  const done = await runBulk("Moving", targets.map((s) => s.id), (id) => api(actionPath(id, "move"), { cwd }));
  if (done.length) toast(hasRemote(done) ? "Local moves done; Mac moves queued. Watch their action status." : `Moved ${plural(done.length, "session")} to ${cwd}.`);
}

async function onAction(el) {
  const id = el.dataset.id, s = id && byId(id);
  if (s?.host && el.dataset.act.startsWith("host-")) return hostAction(el, s, refresh);
  if (s?.host && !["select", "expand", "copy", "delete"].includes(el.dataset.act)) return toast("This action is unavailable on the Mac.");
  switch (el.dataset.act) {
    case "select": sel.toggle(id); return render();
    case "expand":
      expanded.has(id) ? expanded.delete(id) : expanded.add(id);
      sel.moveCursor(id);
      return render();
    case "delete": return deleteSessions([id]);
    case "move": return moveSessions([id], $("cwd-" + id).value);
    case "menu":
      menu = menu?.id === id ? null : { id, cwd: $("cwd-" + id).value };
      render();
      return document.querySelector(".menu button")?.focus();
    case "fork": return forkSession(id, menu?.cwd ?? $("cwd-" + id)?.value);
    case "screen":
      try { screens.set(id, (await api(`/api/sessions/${id}/screen`)).screen); }
      catch (e) { screens.set(id, "error: " + e.message); }
      return render();
    case "screen-close":
      screens.delete(id);
      return render();
    case "peek":
      try { peeks.set(id, (await api(`/api/sessions/${id}/messages`)).messages); }
      catch (e) { peeks.set(id, "Couldn't load messages: " + e.message); }
      return render();
    case "restart":
      if (s.status?.busy && !await confirmDialog({ title: "Restart it?", body: "It's working on a turn right now.", confirm: "Restart", danger: true })) return;
      return act(id, "restart");
    case "name":
      el.blur();
      return act(id, "name", { name: $("name-" + id).value });
    case "rename": {
      const title = $("title-" + id).value.trim();
      el.blur();
      if (title && title !== s.title && await act(id, "rename", { title })) toast(`Renamed to “${title}”.`);
      return;
    }
    case "copy":
      return toast((await copyText(el.dataset.text)) ? "Copied." : "Couldn't copy; select the text instead.");
  }
}

// ---- events ----------------------------------------------------------------

function onCardClick(id, ev) {
  const mod = ev.metaKey || ev.ctrlKey;
  if (ev.shiftKey) sel.extendTo(order, id, mod);
  else if (mod) sel.toggle(id);
  else sel.only(id);
  render();
}

$("list").addEventListener("mousedown", (ev) => {
  // Shift-click would otherwise select page text.
  if (ev.shiftKey && ev.target.closest("[data-card]") && !ev.target.closest("input, .panel")) ev.preventDefault();
});

$("list").addEventListener("click", (ev) => {
  const link = ev.target.closest(".dirlink");
  if (link) return setDir(link.dataset.dir === dir ? null : link.dataset.dir);
  const actEl = ev.target.closest("[data-act]");
  if (actEl && actEl.dataset.act !== "toggle") return onAction(actEl);
  if (ev.target.closest("a, button, input, label, .panel")) return;
  const cardEl = ev.target.closest("[data-card]");
  if (cardEl) onCardClick(cardEl.dataset.card, ev);
});

$("list").addEventListener("change", async (ev) => {
  const el = ev.target;
  if (el.dataset.act !== "toggle") return;
  const s = byId(el.dataset.id);
  if (!el.checked && s.status?.busy && !await confirmDialog({
    title: "Stop keeping it running?", body: "It's working on a turn right now.", confirm: "Stop", danger: true })) {
    el.checked = true;
    return;
  }
  await act(s.id, el.checked ? "enable" : "disable");
});

$("dirs").addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-dir]");
  if (!el) return;
  const d = el.hasAttribute("data-all") ? null : el.dataset.dir;
  setDir(d === dir ? null : d);
});

$("bulkbar").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-bulk]");
  if (!b || bulkProgress) return;
  const ids = sel.list(order);
  switch (b.dataset.bulk) {
    case "keep": return setKept(ids, true);
    case "stop": return setKept(ids, false);
    case "move": return moveSessions(ids);
    case "delete": return deleteSessions(ids);
    case "all": sel.selectAll(order); return render();
    case "clear": sel.clear(); return render();
  }
});

$("trash-list").addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-restore]");
  if (el) restoreMany([el.dataset.restore]);
});

$("trash-empty").addEventListener("click", async () => {
  if (!await confirmDialog({ title: `Permanently delete ${plural(trash.filter((t) => !t.host).length, "Pi session")}?`,
                             body: "This clears only the Pi trash and can't be undone. Mac trash is managed on the Mac.", confirm: "Delete forever", danger: true })) return;
  try { await api("/api/trash/empty", {}); toast("Pi trash emptied. Mac trash stays on the Mac."); }
  catch (e) { toast(`Couldn't empty the trash: ${e.message}`); }
  refresh();
});

function scrollToCursor() {
  document.querySelector(`[data-card="${sel.cursor}"]`)?.scrollIntoView({ block: "nearest" });
}

// A click anywhere outside the open menu closes it.
document.addEventListener("click", (ev) => {
  if (menu && !ev.target.closest(".split")) { menu = null; render(); }
});

// Keyboard selection. Ignored while typing in a field or while a dialog is open.
document.addEventListener("keydown", (ev) => {
  if (!data || document.querySelector("dialog[open]")) return;
  if (ev.key === "Escape" && menu) {
    const id = menu.id;
    menu = null;
    render();
    return document.querySelector(`[data-act=menu][data-id="${id}"]`)?.focus();
  }
  const t = ev.target;
  if (t.matches?.("input, textarea, select")) {
    if (ev.key === "Escape" && t.id === "q") t.blur();
    return;
  }
  const mod = ev.metaKey || ev.ctrlKey;
  const down = ev.key === "ArrowDown" || ev.key === "j", up = ev.key === "ArrowUp" || ev.key === "k";
  if ((down || up) && !mod && !ev.altKey) {
    const id = sel.step(order, down ? 1 : -1);
    if (!id) return;
    ev.preventDefault();
    ev.shiftKey ? sel.extendTo(order, id) : sel.moveCursor(id);
    render();
    return scrollToCursor();
  }
  // Let focused buttons and links handle their own Space/Enter.
  if ((ev.key === " " || ev.key === "Enter") && t.matches?.("button, a, summary")) return;
  if (ev.key === " " && sel.cursor) {
    ev.preventDefault();
    sel.toggle(sel.cursor);
    render();
  } else if (ev.key === "Enter" && sel.cursor) {
    ev.preventDefault();
    expanded.has(sel.cursor) ? expanded.delete(sel.cursor) : expanded.add(sel.cursor);
    render();
  } else if (ev.key === "Escape" && sel.size) {
    sel.clear();
    render();
  } else if ((ev.key === "Delete" || ev.key === "Backspace") && !mod) {
    const ids = sel.size ? sel.list(order) : sel.cursor ? [sel.cursor] : [];
    if (!ids.length) return;
    ev.preventDefault();
    deleteSessions(ids);
  } else if (ev.key.toLowerCase() === "a" && mod) {
    ev.preventDefault();
    sel.selectAll(order);
    render();
  } else if (ev.key === "/") {
    ev.preventDefault();
    $("q").focus();
  } else if (ev.key === "n" && !mod && !ev.altKey) {
    ev.preventDefault();
    newSession();
  }
});

for (const [btn, value] of Object.entries(TABS)) {
  $(btn).addEventListener("click", () => {
    tab = value;
    for (const b of Object.keys(TABS)) $(b).classList.toggle("on", b === btn);
    render();
  });
}
$("q").addEventListener("input", () => {
  query = compileQuery($("q").value);
  render();
});
$("new-btn").addEventListener("click", () => newSession());
for (const [btn, value] of Object.entries(TOOLS)) {
  $(btn).classList.toggle("on", value === tool);
  $(btn).addEventListener("click", () => {
    tool = value;
    try { localStorage.setItem("switchboard.tool", tool); } catch {}
    for (const b of Object.keys(TOOLS)) $(b).classList.toggle("on", b === btn);
    render();
  });
}
setHTML($("sort"), SORTS.map((s) => `<option value="${s.key}">${s.label}</option>`).join(""));
$("sort").value = SORTS.some((s) => s.key === sort) ? sort : DEFAULT_SORT;
$("sort").addEventListener("change", () => {
  sort = $("sort").value;
  try { localStorage.setItem("switchboard.sort", sort); } catch {}
  render();
});
$("help-btn").addEventListener("click", () => {
  $("help").hidden = !$("help").hidden;
  $("help-btn").setAttribute("aria-expanded", String(!$("help").hidden));
});

fulltext = initFullText({ api, setHTML, esc, age, copyText, toast, normalRender: render });
refresh();
setInterval(refresh, 4000);
