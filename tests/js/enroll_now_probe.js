// The page half of test_enroll_now_wording_page_and_cli: the real page,
// one candidate, Enroll now tapped; prints the confirmation and the button.
"use strict";
const path = require("node:path");
const { paired, snap, settle } = require(path.join(process.argv[2], "page_harness.js"));
(async () => {
  const h = await paired(snap({ candidates: [{ host: "mac", session_id: "c1", label: "x", folder: "", seen: 1 }] }));
  const list = h.$("candidate-list");
  const by = (id) => list.all((k) => k.getAttribute("data-testid") === id)[0];
  const button = by("enroll-now").textContent;
  by("enroll-now").click();
  await settle();
  process.stdout.write(JSON.stringify({ button, notice: by("enroll-now-text").textContent,
    confirm: by("enroll-now-yes").textContent }));
})().catch((e) => { console.error(e); process.exit(1); });
