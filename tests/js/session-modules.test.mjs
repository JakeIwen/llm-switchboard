import test from "node:test";
import assert from "node:assert/strict";
import { BulkActions, splitBlocked } from "../../llm_switchboard/static/session-actions.js";
import { createSessionCards, sessionBlockedReason } from "../../llm_switchboard/static/session-cards.js";
import { Selection } from "../../llm_switchboard/static/selection.js";
import { hostBadge } from "../../llm_switchboard/static/hosts.js";

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
  assert.doesNotMatch(html, /class="session-tags"/);
});

test("cards without a keep switch pair their machine and tool tags", () => {
  const cards = createSessionCards(new Selection(), new Set(), new Map(), new Map(), (row) => row.project, () => null);
  const base = { id:"a", title:"Alpha", project:"~/alpha", cwd:"/tmp/alpha", short_id:"a", size:12,
    mtime:0, enabled:false, open_elsewhere:[], host_online:true, mutable:true, state:"closed" };
  for (const [fields, expected] of [
    [{ tool:"codex" }, [["A Codex session", "Codex"], ["This Pi", "Pi"]]],
    [{ host:'mac<&"', tool:"claude" }, [["mac&lt;&amp;&quot;", "Mac"], ["A Claude Code session", "Claude"]]],
    [{ host:"mac", tool:"codex" }, [["mac", "Mac"], ["A Codex session", "Codex"]]],
  ]) {
    const html = cards.render({ ...base, ...fields });
    const tags = [...html.matchAll(/<span class="tool-tag(?: machine-tag)?" title="([^"]*)">([^<]*)<\/span>/g)]
      .map((match) => match.slice(1));
    assert.deepEqual(tags, expected);
    assert.match(html, /class="session-tags"/);
    assert.doesNotMatch(html, /class="switch"/);
  }
});

test("host badges keep the machine and availability without repeating the tool", () => {
  for (const tool of ["claude", "codex"]) {
    assert.equal(hostBadge({ host:"mac", host_online:true, tool }), '<span class="badge b-info">mac · online</span>');
    assert.match(hostBadge({ host:"mac", host_online:false, last_seen:100, tool }),
      /^<span class="badge b-idle">mac · offline, last seen [^<·]+<\/span>$/);
  }
});
