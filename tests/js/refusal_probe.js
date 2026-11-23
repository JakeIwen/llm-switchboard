// Feeds the hub's own answers to a conversation's ticket or send into the
// controller and reports what the page did. tests/test_desk_conversation_js.py
// drives a real HubState into each refusal and checks the result against
// the refusal's ownership class.
// stdin: [{path: "ticket" | "send", status, reason}]
// stdout: [{phase, lost, reason, cue}], `cue` true when the closing cue was spoken.
"use strict";
const { CUE } = require("../../llm_switchboard/desk/static/conversation_logic.js");
const { PATH } = require("../../llm_switchboard/desk/static/conversation.js");
const { harness, listening, texts } = require("./controller_harness.js");

async function probe(c) {
  const h = await listening(harness());
  await h.hear("x");
  await h.hear("send it");
  if (c.path === "send") await h.answer(PATH.TICKET, 200, { ticket: "t1" });
  await h.answer(c.path === "send" ? PATH.SEND : PATH.TICKET, c.status, { error: c.reason });
  const v = h.view();
  return { phase: v.phase, lost: v.lost, reason: v.reason, cue: texts(h).some((t) => t.startsWith(CUE.ENDED)) };
}

let input = "";
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", async () => {
  const out = [];
  for (const c of JSON.parse(input)) out.push(await probe(c));
  process.stdout.write(JSON.stringify(out));
});
