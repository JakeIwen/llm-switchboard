// Drives the picker in headless Chromium against an instance started by run.sh.
import puppeteer from "puppeteer-core";
import fs from "node:fs";

const BASE = process.env.CAR_BASE || "http://127.0.0.1:8796/";
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
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
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
const waitFor = async (fn, ms = 8000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await fn()) return true; await sleep(100); }
  return false;
};

check("loads all sessions", (await count()) === 10, String(await count()));

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
await page.$eval("#q", (el) => el.blur());

// --- mouse selection
await clickCard(0);
check("click selects one", (await selected()) === 1 && (await bulkShown()));
await clickCard(2, ["Control"]);
check("ctrl-click adds", (await selected()) === 2);
await clickCard(4, ["Shift"]);
check("shift-click selects range from anchor", (await selected()) === 3, String(await selected()));
await clickCard(6, ["Control", "Shift"]);
check("ctrl+shift-click extends keeping selection", (await selected()) === 5, String(await selected()));
await page.keyboard.press("Escape"); await sleep(30);
check("Escape clears", (await selected()) === 0 && !(await bulkShown()));
await page.click("#list .card .check"); await sleep(30);
check("checkbox selects", (await selected()) === 1);
await page.keyboard.press("Escape"); await sleep(30);

// --- keyboard selection
await page.keyboard.press("ArrowDown"); await page.keyboard.press("ArrowDown"); await sleep(30);
const cursorIdx = await page.$$eval("#list .card", (els) => els.findIndex((e) => e.classList.contains("cursor")));
// The checkbox click left the cursor on row 0; Escape keeps the cursor.
check("arrows move the cursor", cursorIdx === 2, String(cursorIdx));
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
const trashDir = `${HOME}/.local/share/claude-always-remote/trash`;
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
await page.keyboard.press("n"); await sleep(150);
check("n opens the new-session dialog", await dialogOpen());
const options = await page.$$eval("dialog[open] select option", (els) => els.map((e) => e.textContent));
check("only trusted folders are offered", options.length === 2 && options.every((o) => o.includes("~/dev/")), options.join(" | "));
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
check("gamma folder chip appears", await waitFor(() => page.$$eval("#dirs .chip", (els) => els.some((e) => e.textContent.includes("dev/gamma")))));

// --- single move from the details panel
await page.click("#list .card .more"); await sleep(50);
const id = await page.$eval("#list .card", (e) => e.dataset.card);
await page.$eval(`#cwd-${id}`, (el, v) => { el.value = v; }, `${HOME}/dev/beta`);
await page.click(`[data-act="move"][data-id="${id}"]`);
check("panel Move works", await waitFor(async () => {
  const r = await (await fetch(BASE + "api/sessions")).json();
  return r.sessions.find((s) => s.id === id)?.cwd === `${HOME}/dev/beta`;
}));

// --- empty trash
await page.click("#trash summary"); await sleep(50);
await page.click("#trash-empty"); await sleep(100);
await page.keyboard.press("Enter");
check("Empty now clears the trash", await waitFor(async () => fs.readdirSync(trashDir).length === 0));

if (SHOTS) {
  await clickCard(1); await clickCard(3, ["Control"]);
  await page.screenshot({ path: `${SHOTS}/desktop.png`, fullPage: true });
  await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
  await sleep(200);
  await page.screenshot({ path: `${SHOTS}/mobile.png`, fullPage: false });
}

check("no page errors", errors.length === 0, errors.join(" | "));
await browser.close();
console.log(failures ? `${failures} FAILED` : "ALL PASSED");
process.exit(failures ? 1 : 0);
