import { deviceClass, folderChoices } from "./dirs.js";
import { esc, plural } from "./format.js";

const STORAGE_KEY = "switchboard.dirs";
const LEGACY_KEY = "switchboard.dir";
const $ = (id) => document.getElementById(id);

export function readFolders(storage) {
  try {
    const saved = storage.getItem(STORAGE_KEY);
    if (saved !== null) {
      const keys = JSON.parse(saved);
      return new Set(Array.isArray(keys) ? keys.filter((key) => typeof key === "string" && key) : []);
    }
    const old = storage.getItem(LEGACY_KEY);
    return new Set(old ? [old] : []);
  } catch { return new Set(); }
}

export class FolderFilter {
  constructor({ setHTML, onChange, storage }) {
    this.setHTML = setHTML;
    this.onChange = onChange;
    this.storage = storage;
    this.selected = readFolders(storage);
    this.model = { choices: [], total: 0 };
    this.install();
  }

  get single() { return this.selected.size === 1 ? [...this.selected][0] : null; }
  matches(key) { return !this.selected.size || this.selected.has(key); }

  set(keys) {
    this.selected = new Set(keys);
    try {
      this.storage.setItem(STORAGE_KEY, JSON.stringify([...this.selected]));
      this.storage.removeItem(LEGACY_KEY);
    } catch {}
    this.onChange();
  }

  toggleOnly(key) { this.set(this.single === key ? [] : [key]); }

  install() {
    const dialog = $("folder-dialog");
    $("dir").addEventListener("click", () => this.open());
    $("folder-done").addEventListener("click", () => dialog.close());
    $("folder-close").addEventListener("click", () => dialog.close());
    $("folder-all").addEventListener("click", () => this.set([]));
    $("folder-clear").addEventListener("click", () => { this.set([]); $("dir").focus(); });
    $("folder-search").addEventListener("input", () => this.renderOptions());
    $("folder-options").addEventListener("change", (event) => {
      const key = event.target.dataset.folder;
      if (!key) return;
      const next = new Set(this.selected);
      event.target.checked ? next.add(key) : next.delete(key);
      this.set(next);
    });
    $("folder-chips").addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-folder]");
      if (!button) return;
      this.set([...this.selected].filter((key) => key !== button.dataset.removeFolder));
      $("dir").focus();
    });
    let backdropDown = false;
    dialog.addEventListener("pointerdown", (event) => { backdropDown = event.target === dialog; });
    dialog.addEventListener("click", (event) => {
      if (backdropDown && event.target === dialog) dialog.close();
      backdropDown = false;
    });
    dialog.addEventListener("close", () => {
      document.body.classList.remove("folder-open");
      $("dir").setAttribute("aria-expanded", "false");
      $("dir").focus({ preventScroll: true });
    });
    window.visualViewport?.addEventListener("resize", () => this.fitViewport());
    window.visualViewport?.addEventListener("scroll", () => this.fitViewport());
    window.addEventListener("resize", () => this.fitViewport());
  }

  fitViewport() {
    const dialog = $("folder-dialog"), viewport = window.visualViewport;
    if (!dialog.open || !viewport) return;
    // iOS's keyboard reduces the visual viewport without resizing the page.
    dialog.style.setProperty("--folder-viewport-height", `${viewport.height}px`);
    dialog.style.setProperty("--folder-viewport-bottom", `${Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop)}px`);
  }

  open() {
    $("folder-search").value = "";
    this.renderOptions();
    $("folder-dialog").showModal();
    $("folder-options").scrollTop = 0;
    this.fitViewport();
    document.body.classList.add("folder-open");
    $("dir").setAttribute("aria-expanded", "true");
    // Keep the keyboard out of the way until a phone user chooses to search.
    $("folder-close").focus({ preventScroll: true });
  }

  render(sessions) {
    this.model = folderChoices(sessions, this.selected);
    const picked = this.model.choices.filter((choice) => this.selected.has(choice.key));
    $("dir-label").textContent = picked.length ? plural(picked.length, "folder") : "All folders";
    $("dir-count").textContent = picked.length ? picked.reduce((n, choice) => n + choice.count, 0) : this.model.total;
    $("dir").classList.toggle("on", Boolean(picked.length));
    $("folder-selection").hidden = !picked.length;
    this.setHTML($("folder-chips"), picked.map((choice) => `<button type="button" class="folder-chip ${deviceClass(choice.host)}"
      data-remove-folder="${esc(choice.key)}" aria-label="${esc(`Remove ${choice.project} on ${choice.host || "This Pi"}`)}">
      <span>${esc(choice.project)}</span><small>${esc(choice.host || "This Pi")}</small><span aria-hidden="true">×</span></button>`).join(""));
    $("folder-total").textContent = this.model.total;
    $("folder-all").setAttribute("aria-pressed", String(!picked.length));
    $("folder-status").textContent = picked.length ? `${plural(picked.length, "folder")} selected` : "All folders shown";
    $("folder-clear").disabled = !picked.length;
    this.renderOptions();
  }

  renderOptions() {
    const query = $("folder-search").value.trim().toLocaleLowerCase();
    const choices = this.model.choices.filter((choice) => `${choice.project} ${choice.host || "This Pi"}`.toLocaleLowerCase().includes(query));
    const groups = new Map();
    for (const choice of choices) {
      if (!groups.has(choice.host)) groups.set(choice.host, []);
      groups.get(choice.host).push(choice);
    }
    const list = $("folder-options");
    const focused = document.activeElement?.dataset.folder;
    const scroll = list.scrollTop;
    this.setHTML(list, choices.length ? [...groups].map(([host, items]) => `<fieldset class="folder-group">
      <legend class="${deviceClass(host)}">${esc(host || "This Pi")}</legend>
      ${items.map((choice) => this.option(choice)).join("")}</fieldset>`).join("") : '<p class="folder-empty">No folders match your search.</p>');
    for (const input of list.querySelectorAll("input")) {
      input.checked = this.selected.has(input.dataset.folder);
      if (focused === input.dataset.folder && document.activeElement !== input) input.focus({ preventScroll: true });
    }
    list.scrollTop = scroll;
  }

  option(choice) {
    return `<label class="folder-option">
      <input type="checkbox" data-folder="${esc(choice.key)}">
      <span class="folder-option-text"><span>${esc(choice.project)}</span>${choice.defaultListed ? "" : '<small>Outside default folders</small>'}</span>
      <span class="folder-option-count" aria-label="${plural(choice.count, "session")}">${choice.count}</span>
    </label>`;
  }
}
