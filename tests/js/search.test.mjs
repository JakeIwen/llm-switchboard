import test from "node:test";
import assert from "node:assert/strict";
import { compileQuery } from "../../claude_always_remote/static/search.js";

const NOW = 1_000_000;
const S = (o) => ({ title: "", first_prompt: "", project: "", cwd: "", id: "", rc_name: "", enabled: false,
                    status: null, open_elsewhere: [], mtime: NOW - 60, ...o });
const sessions = [
  S({ id: "c96934f7-aaaa", title: "Telemetrics app changes", project: "~/dev/obd-things", cwd: "/home/pi/dev/obd-things",
      enabled: true, status: { state: "running", busy: true } }),
  S({ id: "0c598c4d-bbbb", title: "Reply with just OK", project: "/tmp", cwd: "/tmp", mtime: NOW - 10 * 86400 }),
  S({ id: "5b80da2b-cccc", title: "Fix the CAN bus decoder", project: "~/dev/obd-things", cwd: "/home/pi/dev/obd-things",
      open_elsewhere: [{ pid: 1, status: "idle" }] }),
];
const ids = (q) => sessions.filter((s) => compileQuery(q).test(s, NOW)).map((s) => s.id.slice(0, 4));

test("plain words are ANDed and case-insensitive", () => {
  assert.deepEqual(ids("obd"), ["c969", "5b80"]);
  assert.deepEqual(ids("OBD telemetrics"), ["c969"]);
  assert.deepEqual(ids(""), ["c969", "0c59", "5b80"]);
});

test("minus negates", () => {
  assert.deepEqual(ids("-telemetrics"), ["0c59", "5b80"]);
  assert.deepEqual(ids("obd -can"), ["c969"]);
  assert.deepEqual(ids("-dir:tmp"), ["c969", "5b80"]);
  assert.deepEqual(ids('-"just ok"'), ["c969", "5b80"]);
  assert.deepEqual(ids('-"ok just"'), ["c969", "0c59", "5b80"]);
  assert.deepEqual(ids("obd -"), ["c969", "5b80"]);
  assert.deepEqual(ids("obd -dir:"), ["c969", "5b80"]);
});

test("quoted phrases", () => {
  assert.deepEqual(ids('"bus decoder"'), ["5b80"]);
  assert.deepEqual(ids('"decoder bus"'), []);
  assert.deepEqual(ids('"unterminated phr'), []);
});

test("field prefixes", () => {
  assert.deepEqual(ids("dir:~/dev"), ["c969", "5b80"]);
  assert.deepEqual(ids("folder:/home/pi"), ["c969", "5b80"]);
  assert.deepEqual(ids("title:ok"), ["0c59"]);
  assert.deepEqual(ids("id:5b80"), ["5b80"]);
  assert.deepEqual(ids("is:kept"), ["c969"]);
  assert.deepEqual(ids("is:off"), ["0c59", "5b80"]);
  assert.deepEqual(ids("is:running"), ["c969"]);
  assert.deepEqual(ids("is:open"), ["c969", "5b80"]);
  assert.deepEqual(ids("is:elsewhere"), ["5b80"]);
  assert.deepEqual(ids("is:busy"), ["c969"]);
  assert.deepEqual(ids("state:running"), ["c969"]);
});

test("age comparisons", () => {
  assert.deepEqual(ids("age:<1d"), ["c969", "5b80"]);
  assert.deepEqual(ids("age:>1w"), ["0c59"]);
  assert.deepEqual(ids("age:2w"), ["c969", "0c59", "5b80"]);
  assert.deepEqual(ids("-age:<1h"), ["0c59"]);
});

test("unknown prefixes are plain text; bad values report errors", () => {
  assert.deepEqual(ids("https://x"), []);
  assert.deepEqual(compileQuery("is:nope").errors.length, 1);
  assert.deepEqual(compileQuery("age:soon").errors.length, 1);
  assert.equal(compileQuery("  - ").empty, true);
});
