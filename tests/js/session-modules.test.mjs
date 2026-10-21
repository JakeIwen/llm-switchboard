import test from "node:test";
import assert from "node:assert/strict";
import { BulkActions, splitBlocked } from "../../llm_switchboard/static/session-actions.js";
import { createSessionCards, sessionBlockedReason } from "../../llm_switchboard/static/session-cards.js";
import { Selection } from "../../llm_switchboard/static/selection.js";

test("bulk actions run sequentially and expose progress", async () => {
  const sessions = new Map([["a", {title:"Alpha"}], ["b", {title:"Beta"}]]);
  const progress = [], calls = [];
  const runner = new BulkActions({
    lookup: (id) => sessions.get(id),
    showProgress: (value) => progress.push(value),
    refresh: async () => calls.push("refresh"),
  });
  const done = await runner.run("Moving", ["a", "b"], async (id) => calls.push(id));
  assert.deepEqual(done, ["a", "b"]);
  assert.deepEqual(calls, ["a", "b", "refresh"]);
  assert.deepEqual(progress, ["Moving 1 of 2…", "Moving 2 of 2…", ""]);
});

test("blocked partition and session cards retain their public contracts", () => {
  const rows = new Map([
    ["a", {id:"a", title:"Alpha", project:"~/alpha", cwd:"/tmp/alpha", short_id:"a", size:12, mtime:0,
      enabled:true, status:{state:"running",rss_kb:2048}, open_elsewhere:[], tool:"claude", rc_name:"Alpha"}],
    ["b", {id:"b", title:"Beta", open_elsewhere:[{pid:42}]}],
  ]);
  assert.deepEqual(splitBlocked(["a", "b"], (id) => rows.get(id), sessionBlockedReason).targets.map((row) => row.id), ["a"]);
  const selection = new Selection(); selection.only("a");
  const html = createSessionCards(selection, new Set(), new Map(), new Map(), (row) => row.project, () => null).render(rows.get("a"));
  assert.match(html, /class="card enabled selected cursor"/);
  assert.match(html, /Remote Control on/);
  assert.match(html, /data-act="toggle"/);
  assert.match(html, /class="dirlink dev-local"/);
  assert.match(html, /title="On this Pi; show only this folder"/);
});
