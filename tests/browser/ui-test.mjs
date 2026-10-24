// Drives the picker in headless Chromium against an instance started by run.sh.
import puppeteer from "puppeteer-core";
import fs from "node:fs";

const BASE = process.env.SWITCHBOARD_BASE || "http://127.0.0.1:8796/";
const HOME = process.argv[2];
const SHOTS = process.argv[3];
let failures = 0;
const check = (name, cond, extra = "") => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra ? "  — " + extra : ""}`);
  if (!cond) failures++;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.launch({ executablePath: "/usr/bin/chromium", headless: true, args: ["--no-sandbox", "--disable-gpu"], timeout: 90000 });
const page = await browser.newPage();
page.setDefaultTimeout(10000);
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
// A 409 is how the server refuses an action the test expects to be refused.
page.on("console", (m) => m.type() === "error" && !m.text().includes("status of 409") && errors.push(m.text()));
await page.setViewport({ width: 1000, height: 900 });
await page.goto(BASE);
await page.waitForSelector(".card");

const count = () => page.$$eval("#list .card", (els) => els.length);
const selected = () => page.$$eval("#list .card.selected", (els) => els.length);
const titles = () => page.$$eval("#list .card .title", (els) => els.map((e) => e.textContent));
const bulkShown = () => page.$eval("#bulkbar", (el) => !el.hidden);
const dialogOpen = () => page.evaluate(() => Boolean(document.querySelector("dialog[open]")));
const search = async (q) => {
  await page.$eval("#q", (el) => { el.value = ""; el.dispatchEvent(new Event("input")); });
  await page.type("#q", q);
  await sleep(50);
};
const clickCard = async (i, mods = []) => {
  const el = (await page.$$("#list .card .title"))[i];
  for (const m of mods) await page.keyboard.down(m);
  await el.click();
  for (const m of mods) await page.keyboard.up(m);
  await sleep(30);
};
// The 4s poll can replace a node between puppeteer finding it and clicking it.
const clickSettled = async (selector) => {
  for (let i = 0; ; i++) {
    try { return await page.click(selector); }
    catch (e) { if (i === 4 || !/detached/.test(e.message)) throw e; await sleep(100); }
  }
};
const waitFor = async (fn, ms = 8000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(100); }
  return false;
};

const tagsFit = (selector, labels, titles) => page.$$eval(selector, (cards, expected) => cards.length > 0 && cards.every((card) => {
  const column = card.querySelector(".session-tags");
  const tags = [...(column?.querySelectorAll(".tool-tag") || [])];
  if (tags.length !== 2 || column.getBoundingClientRect().width !== 44) return false;
  const boxes = tags.map((tag) => tag.getBoundingClientRect());
  return tags.every((tag, i) => tag.textContent === expected.labels[i] && tag.title === expected.titles[i]
    && tag.scrollWidth <= tag.clientWidth && boxes[i].width === 44
    && boxes[i].left >= 0 && boxes[i].right <= innerWidth)
    && boxes[0].left === boxes[1].left && boxes[0].bottom < boxes[1].top;
}), { labels, titles });

check("Codex sessions are listed too", await waitFor(async () => (await count()) === 12), String(await count()));
if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });
for (const width of [390, 1000]) {
  await page.setViewport({ width, height: 900, isMobile: width < 600, hasTouch: width < 600 });
  // Changing mobile emulation reloads the page; measure after its catalogue arrives.
  await page.waitForSelector("#tools", { visible: true });
  const layout = await page.evaluate(() => {
    const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
    const q = rect("#q"), sort = rect("#sort"), scope = rect(".search-scope"), filters = rect(".toolbar-filters");
    const controls = [...document.querySelectorAll(".toolbar button, .toolbar input, .toolbar select")]
      .filter((element) => element.getClientRects().length);
    return {
      noOverflow: document.documentElement.scrollWidth <= innerWidth,
      searchWidest: q.width > sort.width && q.width > scope.width,
      filtersBelow: filters.top >= q.bottom,
      touchTargets: controls.every((element) => element.getBoundingClientRect().height >= 44),
      desktopRows: innerWidth < 700 || (Math.abs(q.top - sort.top) < 3 && Math.abs(rect("#dir").top - rect("#tools").top) < 1),
      noClippedControls: [...document.querySelectorAll(".toolbar .seg")].every((element) => element.scrollWidth <= element.clientWidth),
    };
  });
  check(`toolbar at ${width}px has a wide search, separate filter row, 44px targets and no horizontal scroll`,
    Object.values(layout).every(Boolean), JSON.stringify(layout));
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/toolbar-${width}.png` });
}
await page.keyboard.press("/");
check("slash still focuses the session search", await page.evaluate(() => document.activeElement.id === "q"));
await page.$eval("#q", (element) => element.blur());
await (await import("./folders-test.mjs")).checkFolders({ page, check, waitFor, shots: SHOTS });
// The checks up to the Codex section are about Claude sessions.
await page.click("#t-claude"); await sleep(50);
check("loads all sessions", (await count()) === 10, String(await count()));

// --- install settings
const settingsCard = await page.$eval("#list .card", (el) => el.dataset.card);
await clickSettled(`[data-card="${settingsCard}"] [data-act=expand]`);
await page.waitForSelector(`[data-card="${settingsCard}"] .cmd`);
await page.click("#settings-btn");
check("settings gear opens the install-wide dialog", await waitFor(dialogOpen)
  && (await page.$eval("dialog[open] h2", (el) => el.textContent)) === "Settings");
await page.$eval('dialog[open] input[name="claude_command"]', (el) => {
  el.value = "bad command"; el.dispatchEvent(new Event("input"));
});
check("settings reject shell commands with spaces", await page.$eval("dialog[open] [value=ok]", (el) => el.disabled));
await page.$eval('dialog[open] input[name="claude_command"]', (el) => {
  el.value = "claudep"; el.dispatchEvent(new Event("input"));
});
await page.click("dialog[open] [value=ok]");
check("saving Claude command updates a Pi card resume line", await waitFor(() =>
  page.$eval(`[data-card="${settingsCard}"] .cmd`, (el) => el.textContent.includes("claudep --resume"))));
await page.waitForFunction(() => !document.getElementById("settings-btn").disabled);
await page.click("#settings-btn");
await page.waitForSelector('dialog[open] input[name="claude_command"]');
check("settings reopen with the saved command", (await page.$eval('dialog[open] input[name="claude_command"]', (el) => el.value)) === "claudep");
await page.$eval('dialog[open] input[name="claude_command"]', (el) => {
  el.value = "claude"; el.dispatchEvent(new Event("input"));
});
await page.click("dialog[open] [value=ok]");
await page.waitForFunction(() => !document.querySelector("dialog[open]") && !document.getElementById("settings-btn").disabled);
await clickSettled(`[data-card="${settingsCard}"] [data-act=expand]`);

// --- search modifiers
await search("-alpha"); check("-alpha excludes", (await count()) === 6, String(await count()));
await search("alpha -can"); check("alpha -can", (await count()) === 3);
await search("dir:beta"); check("dir:beta", (await count()) === 3);
await search('"just ok"'); check('"just ok" phrase', (await count()) === 1);
await search("-dir:~/scratch -dir:beta"); check("double negation", (await count()) === 4, String(await count()));
await search("age:<2h"); check("age:<2h", (await count()) === 2, String(await count()));
await search("is:nope");
check("bad is: value shows an error", (await page.$eval("#qerr", (e) => e.textContent)).includes("is: takes"));
await search("");

// --- transcript full-text search (a separate mode from the list filter)
await page.click("#help-btn");
check("Sessions help names its scope and hides transcript syntax", await page.$eval("#help", (element) =>
  element.innerText.includes("Sessions search") && !element.innerText.includes("Transcripts search")));
const bridged = '"reply about Alpha CAN decoder" is:claude is:kept title:Alpha -state:missing id:abc';
await search(bridged);
await page.keyboard.press("Enter");
check("Enter from Sessions switches scope and runs the translated query", await waitFor(() => page.$("#list .search-card").then(Boolean))
  && await page.$eval("#q", (element) => element.value === '"reply about Alpha CAN decoder" tool:claude'));
check("ignored session terms remain visible after results arrive", await page.$eval("#qerr", (element) =>
  element.textContent === "Ignored inside transcripts: is:kept · title:Alpha · -state:missing · id:abc"));
check("Transcripts help shows only its syntax and correct indexed roles", await page.$eval("#help", (element) =>
  element.innerText.includes("Transcripts search") && !element.innerText.includes("Sessions search")
  && !element.innerText.includes("by:tool") && element.innerText.includes("visible replies")));
check("Transcripts placeholder names the scope and Enter", await page.$eval("#q", (element) =>
  element.placeholder === "Search transcripts — press Enter"));
await page.keyboard.press("Escape");
check("Escape restores the original Sessions query and scope", await page.$eval("#q", (element, expected) =>
  element.value === expected && document.querySelector("#scope-sessions").getAttribute("aria-pressed") === "true", bridged));
await search("Alpha is:codex");
await page.click("#scope-transcripts");
check("choosing Transcripts carries and translates the current text without running it", await page.$eval("#q", (element) =>
  element.value === "Alpha tool:codex") && !await page.$("#list .search-card"));
await page.$eval("#q", (element) => { element.value = ""; element.dispatchEvent(new Event("input")); });
check("clearing an unsubmitted transcript query also restores Sessions", await page.$eval("#q", (element) =>
  element.value === "Alpha is:codex" && document.querySelector("#scope-sessions").getAttribute("aria-pressed") === "true"));
await search("is:kept");
await page.keyboard.press("Enter");
check("an all-session-filter query leaves an explanation instead of searching an empty query", await page.$eval("#qerr", (element) =>
  element.textContent === "Ignored inside transcripts: is:kept") && !await page.$("#list .search-card"));
await page.click("#scope-sessions");
await page.click("#help-btn");
await search("");
await page.click("#scope-transcripts");
await page.type("#q", '"reply about Alpha CAN decoder"');
await page.keyboard.press("Enter");
check("transcript search finds the matching session", await waitFor(() => page.$("#list .search-card").then(Boolean)));
check("transcript result shows an excerpt", (await page.$eval("#list .search-excerpt", (e) => e.textContent)).includes("reply about Alpha CAN decoder"));
await page.click("[data-search-open]");
check("opening a transcript result shows context", await waitFor(() => page.$("#list .search-detail").then(Boolean)));
check("matching context includes a resume command", (await page.$eval("#list .search-detail", (e) => e.textContent)).includes("claude --resume"));
await page.$eval("#q", (el) => { el.value = ""; el.dispatchEvent(new Event("input")); });
check("clearing transcript search returns to the normal list", await waitFor(async () => (await count()) === 10));
await search("alpha -can");
check("normal list filter still works after transcript search", (await count()) === 3);
await search("");

await page.select("#sort", "name"); await sleep(50);
const byName = await titles();
check("Name A–Z sorts the list", byName.join("|") === [...byName].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" })).join("|"), byName.join("|"));
check("sort is remembered", (await page.evaluate(() => localStorage.getItem("switchboard.sort"))) === "name");
await page.select("#sort", "kept"); await sleep(50);
await search("");
await page.$eval("#q", (el) => el.blur());

// --- mouse details and selection
await clickCard(0);
check("plain click opens details without selecting", Boolean(await page.$("#list .card:first-child .panel"))
  && (await selected()) === 0 && !(await bulkShown())
  && await page.$eval("#list .card:first-child", (el) => el.classList.contains("cursor")));
await page.click("#list .card:first-child .panel .cmd"); await sleep(30);
check("clicking expanded panel content leaves it open", Boolean(await page.$("#list .card:first-child .panel")) && (await selected()) === 0);
await clickCard(0);
check("another plain click closes details", !(await page.$("#list .panel")) && (await selected()) === 0);
await page.click("#list .card .more"); await sleep(30);
check("details button still opens without selecting", Boolean(await page.$("#list .card:first-child .panel")) && (await selected()) === 0);
await page.click("#list .card .more"); await sleep(30);
await page.click("#list .card .check"); await sleep(30);
check("circle selects one without opening details", (await selected()) === 1 && (await bulkShown()) && !(await page.$("#list .panel")));
await page.click("#list .card .check"); await sleep(30);
check("circle toggles selection off", (await selected()) === 0 && !(await bulkShown()));
await page.click("#list .card .check"); await sleep(30);
await clickCard(2, ["Control"]);
check("ctrl-click adds", (await selected()) === 2);
await clickCard(2, ["Control"]);
check("ctrl-click toggles selection off", (await selected()) === 1);
await clickCard(2, ["Meta"]);
check("cmd-click adds", (await selected()) === 2);
await clickCard(4, ["Shift"]);
check("shift-click selects range from anchor", (await selected()) === 3, String(await selected()));
await clickCard(6, ["Control", "Shift"]);
check("ctrl+shift-click extends keeping selection", (await selected()) === 5, String(await selected()));
check("modifier clicks do not expand cards", !(await page.$("#list .panel")));
await clickCard(1);
check("opening details preserves the selection", (await selected()) === 5 && Boolean(await page.$("#list .panel")));
await clickCard(1);
await page.keyboard.press("Escape"); await sleep(30);
check("Escape clears", (await selected()) === 0 && !(await bulkShown()));
await page.click("#list .card .check"); await sleep(30);
check("circle selects", (await selected()) === 1);
await page.keyboard.press("Escape"); await sleep(30);

// --- keyboard selection
await page.keyboard.press("ArrowDown"); await page.keyboard.press("ArrowDown"); await sleep(30);
const cursorIdx = await page.$$eval("#list .card", (els) => els.findIndex((e) => e.classList.contains("cursor")));
// The checkbox click left the cursor on row 0; Escape keeps the cursor.
check("arrows move the cursor", cursorIdx === 2, String(cursorIdx));
await page.keyboard.press("Enter"); await sleep(30);
check("Enter still opens details without selecting", Boolean(await page.$("#list .card.cursor .panel")) && (await selected()) === 0);
await page.keyboard.press("Enter"); await sleep(30);
check("Enter toggles details closed", !(await page.$("#list .panel")));
await page.keyboard.press("Space"); await sleep(30);
check("Space selects the cursor row", (await selected()) === 1);
await page.keyboard.down("Shift"); await page.keyboard.press("ArrowDown"); await page.keyboard.up("Shift"); await sleep(30);
check("Shift+ArrowDown extends", (await selected()) === 2);
await page.keyboard.down("Control"); await page.keyboard.press("a"); await page.keyboard.up("Control"); await sleep(30);
check("Ctrl+A selects all visible", (await selected()) === 10);
await page.keyboard.press("Escape"); await sleep(30);

// --- keyboard bulk delete, Enter confirms, Undo restores
await page.keyboard.press("ArrowDown"); await page.keyboard.press("Space");
await page.keyboard.down("Shift"); await page.keyboard.press("ArrowDown"); await page.keyboard.up("Shift");
await sleep(30);
const doomed = await page.$$eval("#list .card.selected .title", (els) => els.map((e) => e.textContent));
await page.keyboard.press("Delete"); await sleep(100);
check("Delete key opens a dialog", await dialogOpen());
const dlgTitle = await page.$eval("dialog[open] h2", (e) => e.textContent);
check("dialog names the count", dlgTitle === "Delete 2 sessions?", dlgTitle);
if (SHOTS) await page.screenshot({ path: `${SHOTS}/dialog.png` });
await page.keyboard.press("Enter");
check("Enter confirms and deletes", await waitFor(async () => (await count()) === 8));
check("deleted ones are gone", !(await titles()).some((t) => doomed.includes(t)));
const trashDir = `${HOME}/.local/share/llm-switchboard/trash`;
check("transcripts are in the trash", fs.readdirSync(trashDir).length === 2);
check("toast offers Undo", await page.$eval("#toast-action", (e) => !e.hidden && !e.parentElement.hidden));
await page.click("#toast-action");
check("Undo restores both", await waitFor(async () => (await count()) === 10));

// --- the X button; Cancel and Escape don't delete
const first = (await titles())[0];
await page.click("#list .card .x"); await sleep(100);
check("X opens a dialog", await dialogOpen());
await page.keyboard.down("Shift"); await page.keyboard.press("Tab"); await page.keyboard.up("Shift");
await page.keyboard.press("Enter"); await sleep(300);
check("Enter on a focused Cancel cancels", !(await dialogOpen()) && (await count()) === 10);
await page.click("#list .card .x"); await sleep(100);
await page.keyboard.press("Escape"); await sleep(300);
check("Escape cancels", !(await dialogOpen()) && (await count()) === 10);
await page.click("#list .card .x"); await sleep(100);
await page.keyboard.press("Enter");
check("X + Enter deletes that one", await waitFor(async () => (await count()) === 9 && !(await titles()).includes(first)));

// --- new session: trusted folders only, name required, Enter creates
const before = await count();
await page.keyboard.press("n");
check("n opens the new-session dialog", await waitFor(dialogOpen));
const options = await page.$$eval("dialog[open] select[name=cwd] option", (els) => els.map((e) => e.textContent));
check("folders with sessions are offered", options.length === 3 && options.some((o) => o.includes("~/scratch")), options.join(" | "));
check("tool defaults to Claude Code", (await page.$eval("dialog[open] select[name=tool]", (e) => e.value)) === "claude");
check("first message is hidden for Claude", await page.$eval("dialog[open] input[name=prompt]", (e) => e.closest(".dlg-field").hidden));
await page.select("dialog[open] select[name=cwd]", `${HOME}/scratch`); await sleep(50);
check("an untrusted folder is refused for Claude", (await page.$eval("dialog[open] .dlg-error", (e) => e.textContent)).includes("can't start here"));
await page.select("dialog[open] select[name=cwd]", `${HOME}/dev/alpha`); await sleep(50);
check("untrusted folder is explained", (await page.$eval("dialog[open] .dlg-body", (e) => e.textContent)).includes("~/scratch"));
check("Create is disabled without a name", await page.$eval("dialog[open] [value=ok]", (e) => e.disabled));
await page.keyboard.press("Enter"); await sleep(150);
check("Enter does nothing without a name", await dialogOpen());
await page.type("dialog[open] input[name=name]", "Brand new thing");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/new-session.png` });
const nameHeight = await page.$eval("dialog[open] input[name=name]", (e) => e.getBoundingClientRect().height);
check("name field is one line tall", nameHeight < 60, String(nameHeight));
await page.keyboard.press("Enter");
check("Enter creates it", await waitFor(async () => !(await dialogOpen()) && (await count()) === before + 1));
check("it shows as new and kept running", await page.$$eval("#list .card", (els) => els.some((e) =>
  e.textContent.includes("Brand new thing") && e.textContent.includes("New") && e.classList.contains("enabled"))));
// claudepd doesn't exist under the fake HOME, so the launch fails and the page says so.
check("a failed start is reported", await waitFor(async () =>
  (await page.$eval("#toast-msg", (e) => e.textContent)).includes("needs attention"), 30000));
const newId = await page.$$eval("#list .card", (els) => els.find((e) => e.textContent.includes("Brand new thing")).dataset.card);
await clickSettled(`[data-card="${newId}"] .more`); await sleep(100);
await clickSettled(`[data-card="${newId}"] [data-act=screen]`);
check("Terminal screen opens", await waitFor(() => page.$(`[data-card="${newId}"] .screen pre`).then(Boolean)));
await clickSettled(`[data-card="${newId}"] .screen-close`);
check("× closes the terminal screen", await waitFor(async () => !(await page.$(`[data-card="${newId}"] .screen`))
  && (await page.$eval(`[data-card="${newId}"] [data-act=screen]`, (e) => e.textContent)) === "Terminal screen"));
await page.click(`[data-card="${newId}"] .x`); await sleep(100);
await page.keyboard.press("Enter");
check("deleting a never-started session removes it", await waitFor(async () => (await count()) === before));

// --- bulk move to another folder
await search("dir:~/scratch");
await page.$eval("#q", (el) => el.blur());
await page.keyboard.down("Control"); await page.keyboard.press("a"); await page.keyboard.up("Control"); await sleep(30);
const moving = await selected();
await page.click('[data-bulk="move"]'); await sleep(100);
check("Move opens a prompt", await dialogOpen());
await page.$eval("dialog[open] .dlg-input", (el) => { el.value = ""; });
await page.type("dialog[open] .dlg-input", `${HOME}/dev/gamma`);
await page.keyboard.press("Enter");
const moved = await waitFor(async () => {
  const r = await (await fetch(BASE + "api/sessions")).json();
  return r.sessions.filter((s) => s.cwd === `${HOME}/dev/gamma`).length === moving;
});
check(`bulk move of ${moving} into dev/gamma`, moved && moving === 3, String(moving));
const gammaDir = `${HOME}/.claude/projects/${`${HOME}/dev/gamma`.replace(/[^a-zA-Z0-9]/g, "-")}`;
check("transcripts live under the gamma project folder", fs.existsSync(gammaDir) && fs.readdirSync(gammaDir).length === moving);
await search("");
await page.$eval("#q", (el) => el.blur());
check("gamma folder appears in the folder menu", await waitFor(() => page.$$eval("#folder-options input", (els) => els.some((e) => e.dataset.folder === "~/dev/gamma"))));
await page.click("#dir");
await page.click('[data-folder="~/dev/gamma"]');
await page.click("#folder-done");
check("picking it shows only its sessions", await waitFor(() => page.$$eval("#list .card .dirlink", (els) =>
  els.length === 3 && els.every((e) => e.textContent === "~/dev/gamma"))));
check("the picked filter is marked and its chip tinted", await page.$eval("#dir", (el) => el.matches(".on"))
  && Boolean(await page.$("#folder-chips .dev-local")));
await page.click("#folder-reset");
await page.$eval("#folder-reset", (el) => el.blur());

// --- single move from the details panel
await page.click("#list .card .more"); await sleep(50);
const id = await page.$eval("#list .card", (e) => e.dataset.card);
await page.$eval(`#cwd-${id}`, (el, v) => { el.value = v; }, `${HOME}/dev/beta`);
await page.click(`[data-act="move"][data-id="${id}"]`);
check("panel Move works", await waitFor(async () => {
  const r = await (await fetch(BASE + "api/sessions")).json();
  return r.sessions.find((s) => s.id === id)?.cwd === `${HOME}/dev/beta`;
}));

// --- Move ▾ menu: Fork, and names unique per folder
const caret = `[data-act="menu"][data-id="${id}"]`;
const menuShown = () => page.evaluate(() => Boolean(document.querySelector(".menu")));
await clickSettled(caret); await sleep(100);
check("the caret opens a menu", await menuShown());
if (SHOTS) await page.screenshot({ path: `${SHOTS}/move-menu.png` });
await page.keyboard.press("Escape"); await sleep(100);
check("Escape closes the menu", !(await menuShown()));
await clickSettled(caret); await sleep(100);
await clickSettled("#summary"); await sleep(100);
check("a click elsewhere closes the menu", !(await menuShown()));
await clickSettled(caret); await sleep(100);
await clickSettled(`.menu [data-act="fork"]`); await sleep(150);
check("Fork opens a dialog", await dialogOpen());
const forkName = await page.$eval("dialog[open] input[name=title]", (e) => e.value);
check("fork name is prefilled", forkName.endsWith("(fork)"), forkName);
check("fork folder is the session's", (await page.$eval("dialog[open] input[name=cwd]", (e) => e.value)) === `${HOME}/dev/beta`);
await page.$eval("dialog[open] input[name=title]", (el) => { el.value = ""; });
await page.type("dialog[open] input[name=title]", "beta RELEASE notes");
check("a name taken in that folder is refused", await page.$eval("dialog[open] [value=ok]", (e) => e.disabled)
  && (await page.$eval("dialog[open] .dlg-error", (e) => e.textContent)).includes("already has a session named"));
if (SHOTS) await page.screenshot({ path: `${SHOTS}/fork-dialog.png` });
await page.$eval("dialog[open] input[name=title]", (el) => { el.value = ""; });
await page.type("dialog[open] input[name=title]", "Forked copy");
await page.keyboard.press("Enter");
const forked = await waitFor(async () => {
  const r = await (await fetch(BASE + "api/sessions")).json();
  const f = r.sessions.find((s) => s.title === "Forked copy");
  return f && f.cwd === `${HOME}/dev/beta` && !f.enabled && r.sessions.some((s) => s.id === id);
});
check("Fork makes a copy in that folder and keeps the original", forked, forked ? "" : JSON.stringify(
  (await (await fetch(BASE + "api/sessions")).json()).sessions.filter((s) => s.title === "Forked copy" || s.id === id)
    .map((s) => ({ id: s.id, title: s.title, cwd: s.cwd, enabled: s.enabled }))));
check("the page confirms the fork", await waitFor(async () =>
  (await page.$eval("#toast-msg", (e) => e.textContent)).includes("Forked as “Forked copy”")));
await page.$eval(`#title-${id}`, (el) => { el.value = "forked COPY"; });
await clickSettled(`[data-act="rename"][data-id="${id}"]`);
check("renaming to a taken name is refused", await waitFor(async () =>
  (await page.$eval("#toast-msg", (e) => e.textContent)).includes("already has a session named")),
  await page.$eval("#toast-msg", (e) => e.textContent));

// --- Codex sessions
const api = async () => (await (await fetch(BASE + "api/sessions")).json()).sessions;
await page.click("#t-codex"); await sleep(100);
check("Codex filter shows only Codex sessions", (await count()) === 2);
check("Codex rows have no keep-running switch", (await page.$$("#list .switch")).length === 0);
await page.setViewport({ width: 390, height: 844 }); await sleep(50);
check("Codex and Pi tags stack in a 44px column at 390px", await tagsFit("#list .card", ["Codex", "Pi"], ["A Codex session", "This Pi"]));
check("Codex cards fit the phone width", await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
if (SHOTS) await page.screenshot({ path: `${SHOTS}/cards-codex-mobile.png`, fullPage: true });
await page.setViewport({ width: 1000, height: 900 });
await page.click("#t-all"); await sleep(50);
await search("is:codex"); check("is:codex", (await count()) === 2);
await search("is:codex dir:alpha");
const cx = await page.$eval("#list .card", (e) => e.dataset.card);
await clickSettled(`[data-act="expand"][data-id="${cx}"]`); await sleep(100);
check("Codex panel offers Fork, not Move", Boolean(await page.$(`[data-act="fork"][data-id="${cx}"]`))
  && !(await page.$(`[data-act="move"][data-id="${cx}"]`)) && !(await page.$(`#name-${cx}`)));
await page.$eval(`#title-${cx}`, (el) => { el.value = "fix ALPHA login bug"; });
await clickSettled(`[data-act="rename"][data-id="${cx}"]`);
check("a Codex rename can't take a Claude session's name", await waitFor(async () =>
  (await page.$eval("#toast-msg", (e) => e.textContent)).includes("already has a session named")));
await page.$eval(`#title-${cx}`, (el) => { el.value = "Codex alpha v2"; });
await clickSettled(`[data-act="rename"][data-id="${cx}"]`);
check("renames a Codex session", await waitFor(async () => (await api()).find((s) => s.id === cx)?.title === "Codex alpha v2"));
await clickSettled(`[data-act="fork"][data-id="${cx}"]`); await sleep(150);
check("Codex fork dialog has no keep-running box", await dialogOpen() && !(await page.$("dialog[open] input[name=keep]")));
await page.$eval("dialog[open] input[name=title]", (el) => { el.value = ""; });
await page.type("dialog[open] input[name=title]", "Codex alpha fork");
await page.keyboard.press("Enter");
check("forks a Codex session", await waitFor(async () => (await api()).some((s) => s.tool === "codex" && s.title === "Codex alpha fork")),
  `dialog open: ${await dialogOpen()}; toast: ${await page.$eval("#toast-msg", (e) => e.textContent)}`);
await search("is:codex");
await page.$eval("#q", (el) => el.blur());
const doomedCx = await page.$$eval("#list .card", (els) => els.find((e) => e.textContent.includes("investigate the beta flake")).dataset.card);
await clickSettled(`[data-card="${doomedCx}"] .x`); await sleep(100);
await page.keyboard.press("Enter");
check("deletes a Codex session", await waitFor(async () => !(await api()).some((s) => s.id === doomedCx)));
check("it's in Recently deleted", await waitFor(() => page.$eval("#trash-list", (e) => e.textContent.includes("investigate the beta flake"))));
await page.click("#toast-action");
check("Undo brings the Codex session back", await waitFor(async () => (await api()).some((s) => s.id === doomedCx)));
await page.click("#new-btn");
check("New button opens the Codex session dialog", await waitFor(dialogOpen));
await page.waitForSelector("dialog[open] select[name=tool]");
await page.select("dialog[open] select[name=tool]", "codex"); await sleep(50);
check("Codex asks for a first message", !(await page.$eval("dialog[open] input[name=prompt]", (e) => e.closest(".dlg-field").hidden)));
await page.type("dialog[open] input[name=name]", "Codex new thing");
check("Create waits for the first message", await page.$eval("dialog[open] [value=ok]", (e) => e.disabled));
await page.type("dialog[open] input[name=prompt]", "add a logger");
if (SHOTS) await page.screenshot({ path: `${SHOTS}/new-codex.png` });
await page.keyboard.press("Enter");
check("creates a Codex session that starts working", await waitFor(async () =>
  (await api()).some((s) => s.tool === "codex" && s.title === "Codex new thing" && s.codex_state === "active")));
check("it shows as Working", await waitFor(() => page.$$eval("#list .card", (els) =>
  els.some((e) => e.textContent.includes("Codex new thing") && e.textContent.includes("Working")))));
if (SHOTS) await page.screenshot({ path: `${SHOTS}/codex.png`, fullPage: true });
await search("");
await page.$eval("#q", (el) => el.blur());

// --- empty trash
await page.click("#trash summary"); await sleep(50);
await page.click("#trash-empty"); await sleep(100);
await page.keyboard.press("Enter");
check("Empty now clears the trash", await waitFor(async () => fs.readdirSync(trashDir).length === 0));

if (SHOTS) {
  await page.click("#list .card:nth-child(2) .check"); await clickCard(3, ["Control"]);
  await page.screenshot({ path: `${SHOTS}/desktop.png`, fullPage: true });
  await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
  await sleep(200);
  await page.screenshot({ path: `${SHOTS}/mobile.png`, fullPage: false });
}

await (await import("./hosts-test.mjs")).checkHosts({ page, home: HOME, check, waitFor });
await page.setViewport({ width: 390, height: 844 }); await sleep(50);
check("Mac and Claude tags stack in a 44px column at 390px", await tagsFit('#list [data-card^="mac:"]', ["Mac", "Claude"], ["mac", "A Claude Code session"]));
check("Mac badge does not repeat the tool", await page.$eval('#list [data-card^="mac:"] .badge', (el) => el.textContent === "mac · online"));
check("Mac cards fit the phone width", await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
if (SHOTS) await page.screenshot({ path: `${SHOTS}/cards-mac-mobile.png`, fullPage: true });

check("no page errors", errors.length === 0, errors.join(" | "));
await browser.close();
console.log(failures ? `${failures} FAILED` : "ALL PASSED");
process.exit(failures ? 1 : 0);
