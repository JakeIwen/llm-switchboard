const alpha = '#folder-options input[data-folder="~/dev/alpha"]';
const beta = '#folder-options input[data-folder="~/dev/beta"]';
const count = (page) => page.$$eval("#list .card", (cards) => cards.length);
const input = (page, selector, value) => page.$eval(selector, (element, text) => {
  element.value = text;
  element.dispatchEvent(new Event("input", { bubbles: true }));
}, value);
const total = (page) => page.$eval("#folder-total", (element) => Number(element.textContent));
const open = async (page) => {
  await page.click("#dir");
  await page.waitForSelector("#folder-dialog[open]", { timeout: 3000 });
};

async function selectionChecks({ page, check, waitFor, shots }) {
  check("folder picker uses a button rather than a native select", await page.$eval("#dir", (element) =>
    element.tagName === "BUTTON" && element.getAttribute("aria-haspopup") === "dialog"));
  await open(page);
  check("default folder counts sum to all 12 eligible fixture sessions", await total(page) === 12);
  await page.click(alpha);
  await page.click(beta);
  check("choosing alpha and beta includes both folders and both tools", await waitFor(async () => await count(page) === 9));
  check("multiple choices remain checked while the picker stays open", await page.evaluate(() =>
    document.querySelector("#folder-dialog").open
    && document.querySelectorAll("#folder-options input:checked").length === 2));
  if (shots) await page.screenshot({ path: `${shots}/folder-desktop.png` });
  await page.click("#folder-done");
  check("selected folders appear as removable chips", await page.$$eval("[data-remove-folder]", (elements) => elements.length === 2));
  await page.reload({ waitUntil: "domcontentloaded", timeout: 10000 });
  check("multiple folder selections survive reload", await waitFor(async () => await count(page) === 9));
  await open(page);
  await page.focus(beta);
  await page.keyboard.press("Space");
  check("Space toggles a focused folder without selecting session cards", await waitFor(async () =>
    await count(page) === 5 && await page.$$eval("#list .selected", (elements) => elements.length === 0)));
  await page.keyboard.press("Escape");
  check("Escape closes the picker and returns focus to its trigger", await waitFor(() => page.evaluate(() =>
    !document.querySelector("#folder-dialog").open && document.activeElement.id === "dir")));
  await page.click('[data-remove-folder="~/dev/alpha"]');
  check("removing the last chip restores all folders", await waitFor(async () => await count(page) === 12));
  await open(page);
  await page.click(alpha);
  await page.click(beta);
  await page.click("#folder-all");
  check("All folders clears all choices at once", await waitFor(async () => await count(page) === 12)
    && await page.$$eval("#folder-options input:checked", (elements) => elements.length === 0));
  await page.click(alpha);
  await page.click("#folder-invert");
  check("Invert selects every unselected listed folder and deselects alpha", await waitFor(async () => await count(page) === 7)
    && await page.$$eval("#folder-options input:checked", (elements) => elements.length === 2));
  await input(page, "#folder-search", "beta");
  await page.click("#folder-invert");
  check("Invert within a folder search leaves the other selection untouched", await waitFor(async () => await count(page) === 3));
  await page.click("#folder-invert");
  check("Invert again adds the visible folder back", await waitFor(async () => await count(page) === 7));
  await input(page, "#folder-search", "nothing");
  check("Invert is disabled with no visible choices", await page.$eval("#folder-invert", (element) => element.disabled));
  await page.click("#folder-all");
  await page.click("#folder-done");
}

async function intersectionChecks({ page, check, waitFor }) {
  await open(page);
  await page.click(alpha);
  await page.click("#folder-done");
  await page.click("#t-claude");
  check("tool filter intersects the selected folder", await waitFor(async () => await count(page) === 4));
  check("folder totals follow the tool switch", await total(page) === 10);
  const clearBounds = await page.$eval("#folder-reset", (element) => element.getBoundingClientRect().toJSON());
  await input(page, "#q", "CAN");
  check("list search intersects both folder and tool", await waitFor(async () => await count(page) === 1));
  check("trigger and chip counts follow other filters", await page.$eval("#dir-count", (element) => element.textContent === "1")
    && await page.$eval("#folder-chips .folder-option-count", (element) => element.textContent === "1"));
  check("clear × stays at the right edge of the trigger when counts change", await page.$eval("#folder-reset", (element, before) => {
    const now = element.getBoundingClientRect(), trigger = document.querySelector("#dir").getBoundingClientRect();
    return element.textContent === "×" && now.x === before.x && now.y === before.y && now.right === trigger.right;
  }, clearBounds));
  await open(page);
  check("folder counts follow active list search and tool filters without removing zero counts", await total(page) === 1
    && await page.$$eval("#folder-options .folder-option-count", (elements) => elements.map((element) => element.textContent).join(",") === "1,0,0"));
  await page.type("#folder-search", "beta");
  check("picker search narrows folder choices without changing selected sessions", await page.$$eval("#folder-options input", (elements) =>
    elements.length === 1 && elements[0].dataset.folder === "~/dev/beta") && await count(page) === 1);
  await input(page, "#folder-search", "no-such-folder");
  check("empty folder search has a visible empty state and keeps its filtered total", Boolean(await page.$(".folder-empty")) && await total(page) === 1);
  await input(page, "#folder-search", "");
  check("clearing picker search restores the selected checkbox", await page.$eval(alpha, (element) => element.checked));
  await page.click("#folder-done");
  await input(page, "#q", "");
  await page.click("#f-on");
  check("status tabs intersect selected folders", await waitFor(async () => await count(page) === 0));
  check("kept-running tab updates all folder counts to zero", await total(page) === 0);
  await page.click("#f-open");
  check("open-now tab also counts only matching sessions", await total(page) === 0);
  await page.click("#f-all");
  await page.click("#t-all");
  await page.click("#folder-reset");
  check("fixed × restores all sessions after other filters reset", await waitFor(async () => await count(page) === 12));
  check("fixed × hides once no folders are selected", await page.$eval("#folder-reset", (element) => element.hidden));
  await page.$eval("#dir", (element) => element.blur());
}

function extraCatalogue(catalogue) {
  const rows = [];
  const add = (project, n, host = "") => {
    for (let i = 0; i < n; i++) rows.push({
      ...catalogue.sessions[0], id: `folder-fixture-${rows.length}`, project, host,
      title: `Folder regression ${rows.length}`, enabled: false, open_elsewhere: [],
      host_online: true, last_seen: catalogue.now, state: "closed", mutable: true,
    });
  };
  add("~/dev/alpha", 2, "mac");
  add("~/dev/alpha/worktree", 3);
  add("~/ais", 2);
  add("~/dev/one-off", 1);
  add("/tmp/outside", 2);
  for (let i = 0; i < 14; i++) add(`~/dev/long-project-name-for-phone-${i}`, 2);
  return { ...catalogue, sessions: [...catalogue.sessions, ...rows] };
}

async function geometry(page) {
  return page.evaluate(() => {
    const dialog = document.querySelector("#folder-dialog");
    const bounds = dialog.getBoundingClientRect();
    const done = document.querySelector("#folder-done").getBoundingClientRect();
    const targets = [...dialog.querySelectorAll("button, .folder-option, #folder-search")];
    return {
      inViewport: bounds.left >= 0 && bounds.right <= innerWidth + 1
        && bounds.top >= 0 && bounds.bottom <= innerHeight + 1,
      doneVisible: done.top >= bounds.top && done.bottom <= innerHeight + 1,
      noOverflow: document.documentElement.scrollWidth <= innerWidth && dialog.scrollWidth <= dialog.clientWidth,
      touchTargets: targets.every((element) => element.getBoundingClientRect().height >= 44),
      searchFont: Number.parseFloat(getComputedStyle(document.querySelector("#folder-search")).fontSize),
      scrollable: document.querySelector("#folder-options").scrollHeight > document.querySelector("#folder-options").clientHeight,
    };
  });
}

async function phoneChecks({ page, check, waitFor, shots }) {
  await open(page);
  check("opening on a phone leaves the keyboard closed until search is chosen", await page.evaluate(() => document.activeElement.id !== "folder-search"));
  check("reopening starts at the top of the folder choices", await page.$eval("#folder-options", (element) => element.scrollTop === 0));
  const full = await geometry(page);
  check("iPhone 16 Pro Max sheet fits 440×956 with scrollable options", full.inViewport && full.doneVisible && full.noOverflow && full.scrollable, JSON.stringify(full));
  check("phone folder controls have 44px touch targets and 16px search text", full.touchTargets && full.searchFont >= 16, JSON.stringify(full));
  if (shots) await page.screenshot({ path: `${shots}/folder-mobile.png` });
  await page.type("#folder-search", "mac");
  check("folder search can find a machine", await page.$$eval("#folder-options input", (elements) =>
    elements.length === 1 && elements[0].dataset.folder === "mac:~/dev/alpha"));
  await input(page, "#folder-search", "");
  await page.click(alpha);
  await page.focus(alpha);
  const poll = await page.waitForResponse((response) => new URL(response.url()).pathname === "/api/sessions", { timeout: 12000 });
  await poll.json();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  check("polling preserves an open sheet, its checked choices and keyboard focus", await page.evaluate(() =>
    document.querySelector("#folder-dialog").open
    && document.activeElement.dataset.folder === "~/dev/alpha"
    && document.activeElement.checked));
  for (const [width, height] of [[440, 320], [440, 400], [956, 440]]) {
    await page.setViewport({ width, height, isMobile: true, hasTouch: true });
    await page.focus("#folder-search");
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const resized = await geometry(page);
    check(`phone viewport ${width}×${height} keeps search and Done reachable without horizontal overflow`,
      resized.inViewport && resized.doneVisible && resized.noOverflow && resized.scrollable, JSON.stringify(resized));
  }
  await page.click("#folder-done");
  check("phone selection filters the list", await waitFor(async () => await count(page) === 5));
  await page.click("#folder-reset");
}

async function excludedAndPhoneChecks({ page, check, waitFor, shots }) {
  const catalogue = await page.evaluate(async () => (await fetch("/api/sessions")).json());
  const payload = extraCatalogue(catalogue);
  const isolated = await page.browser().newPage();
  const errors = [];
  isolated.on("pageerror", (error) => errors.push(error.message));
  try {
    await isolated.setViewport({ width: 440, height: 956, isMobile: true, hasTouch: true });
    await isolated.setRequestInterception(true);
    isolated.on("request", (request) => {
      const response = new URL(request.url()).pathname === "/api/sessions"
        ? request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(payload) })
        : request.continue();
      response.catch((error) => { if (!isolated.isClosed()) errors.push(error.message); });
    });
    await isolated.goto(page.url(), { waitUntil: "domcontentloaded", timeout: 10000 });
    await isolated.waitForSelector("#list .card", { timeout: 5000 });
    check("default total excludes deep, one-off, ais and outside-home rows", await total(isolated) === 42 && await count(isolated) === 50);
    await open(isolated);
    await isolated.click(alpha);
    await isolated.click('#folder-options input[data-folder="mac:~/dev/alpha"]');
    check("identical folder paths on Pi and Mac can be selected independently", await waitFor(async () => await count(isolated) === 7)
      && await isolated.$$eval("#folder-options input:checked", (elements) => elements.length === 2));
    await isolated.click("#folder-all");
    await isolated.click("#folder-done");
    await phoneChecks({ page: isolated, check, waitFor, shots });
    payload.settings = { folders: { depth: 3, min_sessions: 1, hidden: [] } };
    check("server folder settings change depth, minimum count and hidden prefixes", await waitFor(async () => await total(isolated) === 48)
      && Boolean(await isolated.$('#folder-options input[data-folder="~/dev/alpha/worktree"]'))
      && Boolean(await isolated.$('#folder-options input[data-folder="~/dev/one-off"]'))
      && Boolean(await isolated.$('#folder-options input[data-folder="~/ais"]')));
    payload.settings.folders.hidden = ["~/dev/alpha"];
    check("updated hidden prefixes exclude descendants on every machine", await waitFor(async () => await total(isolated) === 38)
      && !await isolated.$('#folder-options input[data-folder="mac:~/dev/alpha"]')
      && !await isolated.$('#folder-options input[data-folder="~/dev/alpha/worktree"]'));
    check("folder phone interactions produce no page errors", errors.length === 0, errors.join(" | "));
  } finally {
    await page.evaluate(() => localStorage.setItem("switchboard.dirs", "[]"));
    await isolated.close();
  }
}

export async function checkFolders(context) {
  await selectionChecks(context);
  await intersectionChecks(context);
  await excludedAndPhoneChecks(context);
}
