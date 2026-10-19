import fs from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export async function checkHosts({ page, home, check, waitFor }) {
  const root = `${home}/.local/share/llm-switchboard/hosts`;
  fs.mkdirSync(root, { recursive: true });
  const id = "badc0ffe-0000-4000-8000-000000000001";
  const row = { id, tool: "claude", title: "Mac private project", cwd: "/Users/jacobr/dev/mac-only",
    mtime: 100, size: 40, state: "closed", mutable: true };
  const state = { version: 1, rows: [row], last_seen: Date.now() / 1000 };
  const write = () => fs.writeFileSync(`${root}/m4mac.json`, JSON.stringify(state));
  write();
  const card = `[data-card="m4mac:${id}"]`;
  check("Mac push appears alongside Pi sessions", await waitFor(() => page.$(card).then(Boolean)));
  await page.click(`${card} .more`);
  check("Mac has resume command but no Peek, Terminal or keep switch", await page.$eval(card, (el) =>
    el.textContent.includes("Resume in a shell on the Mac") && el.textContent.includes("claude --resume")
    && !el.querySelector('[data-act="peek"], [data-act="screen"], .switch, [data-act="restart"]')));
  check("host status uses latest push", await page.$eval("#summary", (el) => el.textContent.includes("m4mac: online")));
  state.last_seen -= 36;
  write();
  check("stale Mac rows grey out with last-seen time", await waitFor(() => page.$eval(card, (el) =>
    el.classList.contains("host-offline") && el.textContent.includes("offline, last seen"))));
  check("offline Mac delete is disabled", await page.$eval(`${card} .x`, (el) => el.disabled));
  const exchange = (extra = {}) => JSON.parse(execFileSync("python3", ["-m", "llm_switchboard", "host-exchange", "m4mac"], {
    cwd: fileURLToPath(new URL("../../", import.meta.url)),
    env: { ...process.env, HOME: home, XDG_DATA_HOME: "", XDG_CONFIG_HOME: "" },
    input: JSON.stringify({ version: 1, rows: [row], ...extra }), encoding: "utf8",
  }));
  exchange();
  await waitFor(() => page.$eval(`${card} .x`, (el) => !el.disabled));
  const rename = `${card} [data-act="host-rename"]`;
  await page.$eval(`${card} input[id^="title-"]`, (el) => { el.value = "Mac renamed"; });
  await page.click(rename);
  check("Mac rename queues rather than edits locally", await waitFor(() => page.$eval("#host-actions", (el) => el.textContent.includes("rename: queued"))));
  let action = exchange().actions[0];
  row.title = "Mac renamed";
  exchange({ results: [{ id: action.id, ok: true, message: "Done on the Mac." }] });
  check("Mac result becomes done", await waitFor(() => page.$eval("#host-actions", (el) => el.textContent.includes("rename: done"))));
  await page.$eval(`${card} input[id^="title-"]`, (el) => { el.value = "Another title"; });
  await page.click(rename);
  await waitFor(() => page.$eval("#host-actions", (el) => el.textContent.includes("rename: queued")));
  action = exchange().actions[0];
  exchange({ results: [{ id: action.id, ok: false, message: "Open on the Mac; close it first." }] });
  check("Mac refusal is shown with failed state", await waitFor(() => page.$eval("#host-actions", (el) =>
    el.textContent.includes("rename: failed") && el.textContent.includes("close it first"))));
  await page.$eval(`${card} input[id^="title-"]`, (el) => { el.value = "Expiry test"; });
  await page.click(rename);
  await waitFor(() => page.$eval("#host-actions", (el) => el.textContent.includes("rename: queued")));
  const expiring = JSON.parse(fs.readFileSync(`${root}/m4mac.json`, "utf8"));
  expiring.actions.at(-1).queued_at -= 61;
  fs.writeFileSync(`${root}/m4mac.json`, JSON.stringify(expiring));
  check("undelivered Mac action expires", await waitFor(() => page.$eval("#host-actions", (el) => el.textContent.includes("rename: expired"))));
}
