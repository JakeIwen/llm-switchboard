// Mirrors SearchRole and IndexState in search_records.py.
const ROLE_NAME = { me: "You", reply: "Reply", tool: "Tool", other: "Other" };
const SEARCH_TOOL = { claude: "claude", codex: "codex" };
const INDEX_STATE = { missing: "missing", building: "building", ready: "ready", error: "error" };
const $ = (id) => document.getElementById(id);

class FullTextMode {
  constructor(deps) {
    Object.assign(this, deps);
    this.state = { active: false, searched: false, query: "", results: [], details: new Map(), status: null };
    this.savedListQuery = "";
    this.listPlaceholder = $("q").placeholder;
    this.request = 0;
    this.lastUpdated = null;
  }

  get active() { return this.state.active; }

  install() {
    document.addEventListener("keydown", (event) => {
      if (this.active && event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        this.deactivate();
      }
    }, true);
    $("fulltext-btn").addEventListener("click", () => this.active ? this.deactivate() : this.activate());
    $("q").addEventListener("input", () => {
      if (this.active && this.state.searched && !$("q").value) this.deactivate();
    });
    $("q").addEventListener("keydown", (event) => {
      if (this.active && event.key === "Enter") { event.preventDefault(); this.search(); }
    });
    $("list").addEventListener("click", (event) => this.onResultClick(event));
    return this;
  }

  statusText(status) {
    if (!status) return "Transcript index: checking…";
    const count = status.sessions === 1 ? "1 session" : `${status.sessions} sessions`;
    if (status.state === INDEX_STATE.building) return `Transcript index: updating · ${count}`;
    if (status.state === INDEX_STATE.ready) return `Transcript index: ${count} · updated ${this.age(status.last_updated) || "just now"}`;
    if (status.state === INDEX_STATE.error) return `Transcript index error: ${status.error || "unknown error"}`;
    return "Transcript index: not built yet";
  }

  showStatus(status) {
    this.state.status = status;
    $("index-status").textContent = this.statusText(status);
  }

  activate() {
    if (this.active) return;
    this.state.active = true;
    this.state.searched = false;
    this.savedListQuery = $("q").value;
    $("q").value = "";
    $("q").placeholder = 'Search transcripts  ( words  "phrase"  -exclude  by:me  tool:codex )';
    $("fulltext-btn").classList.add("on");
    $("fulltext-btn").setAttribute("aria-pressed", "true");
    document.body.classList.add("fulltext-mode");
    $("qerr").textContent = "";
    this.setHTML($("list"), '<div class="empty">Type a transcript search and press Enter.</div>');
    $("q").focus();
    this.poll();
  }

  deactivate() {
    if (!this.active) return;
    this.state.active = false;
    this.state.query = "";
    this.state.results = [];
    this.state.details.clear();
    $("q").value = this.savedListQuery;
    $("q").placeholder = this.listPlaceholder;
    $("fulltext-btn").classList.remove("on");
    $("fulltext-btn").setAttribute("aria-pressed", "false");
    document.body.classList.remove("fulltext-mode");
    $("q").dispatchEvent(new Event("input"));
    this.normalRender();
  }

  resultCard(result) {
    const key = `${result.tool}:${result.id}`, detail = this.state.details.get(key);
    const host = result.host ? `<span class="badge b-idle">${this.esc(result.host)}</span>` : "";
    const tool = result.tool === SEARCH_TOOL.codex ? '<span class="tool-tag">Codex</span>' : "";
    const count = result.title_only ? "title/folder match only" : `${result.count} ${result.count === 1 ? "match" : "matches"}`;
    const open = result.title_only ? "" : `<button class="btn" data-search-open="${this.esc(key)}">${detail ? "Hide context" : "Open matches"}</button>`;
    return `<article class="search-card">
      <div class="search-head">${tool}<div class="main"><div class="title">${this.esc(result.title)}</div>
        <div class="meta"><span>${this.esc(result.project)}</span><span>${this.esc(count)}</span><span>${this.age(result.matched_at)}</span><code>${this.esc(result.short_id)}</code></div>
        <div class="badges">${host}</div></div>${open}</div>
      <div class="search-excerpt">${this.esc(result.excerpt)}</div>
      ${detail ? this.detailBlock(detail) : result.title_only && result.resume_cmd ? this.commandLine(result.resume_cmd) : ""}
    </article>`;
  }

  detailBlock(detail) {
    const contexts = detail.contexts.map((context) => `<div class="search-context">${context.lines.map((line) =>
      `<div class="search-line ${line.match ? "match" : ""}"><span>${this.esc(ROLE_NAME[line.role] || line.role)}</span>${this.esc(line.text)}</div>`).join("")}</div>`).join("");
    return `<div class="search-detail">${this.commandLine(detail.result.resume_cmd)}${contexts || '<div class="hint">No context available.</div>'}</div>`;
  }

  commandLine(command) {
    return `<div class="cmdline"><div class="cmd">${this.esc(command)}</div>
      <button class="btn" data-search-copy="${this.esc(command)}">Copy</button></div>`;
  }

  renderResults() {
    if (!this.active) return;
    const html = this.state.results.length ? this.state.results.map((result) => this.resultCard(result)).join("") :
      '<div class="empty">No transcripts match.</div>';
    this.setHTML($("list"), html);
  }

  async search(refresh = false) {
    const query = $("q").value.trim();
    if (!query) return this.state.searched ? this.deactivate() : undefined;
    this.state.searched = true;
    this.state.query = query;
    const token = ++this.request;
    if (!refresh) this.setHTML($("list"), '<div class="empty">Searching transcripts…</div>');
    $("qerr").textContent = "";
    try {
      const payload = await this.api(`/api/search?q=${encodeURIComponent(query)}`);
      if (token !== this.request || !this.active) return;
      this.state.results = payload.results;
      this.showStatus(payload.index);
      this.lastUpdated = payload.index.last_updated;
      this.renderResults();
    } catch (error) {
      if (token !== this.request || !this.active) return;
      $("qerr").textContent = error.message;
      this.setHTML($("list"), `<div class="err">Transcript search failed: ${this.esc(error.message)}</div>`);
    }
  }

  async toggleDetail(key) {
    if (this.state.details.has(key)) {
      this.state.details.delete(key);
      return this.renderResults();
    }
    const [tool, id] = key.split(":", 2);
    try {
      const payload = await this.api(`/api/search/${tool}/${id}?q=${encodeURIComponent(this.state.query)}`);
      this.state.details.set(key, payload.detail);
      this.renderResults();
    } catch (error) {
      this.toast(`Couldn't load matching context: ${error.message}`);
    }
  }

  async onResultClick(event) {
    if (!this.active) return;
    const open = event.target.closest("[data-search-open]");
    if (open) return this.toggleDetail(open.dataset.searchOpen);
    const copy = event.target.closest("[data-search-copy]");
    if (copy) this.toast((await this.copyText(copy.dataset.searchCopy)) ? "Copied." : "Couldn't copy; select the text instead.");
  }

  async poll() {
    // The status line shows only in this mode; activate() fetches it fresh.
    if (!this.active) return;
    try {
      const payload = await this.api("/api/search/status");
      const changed = this.active && this.state.query && payload.index.state === INDEX_STATE.ready &&
        this.lastUpdated !== null && payload.index.last_updated !== this.lastUpdated;
      this.showStatus(payload.index);
      if (changed) this.search(true);
      this.lastUpdated = payload.index.last_updated;
    } catch {}
  }
}

export function initFullText(deps) {
  return new FullTextMode(deps).install();
}
