// Feeds the hub's own answers to a conversation's calls into the controller
// and reports what the page did. tests/test_desk_conversation_js.py drives a
// real HubState into each refusal and checks the result.
// stdin: [{path: "ticket" | "send" | "grant" | "beat", mode: "running" |
// "closing", status, reason}]. "running" answers the call while the
// conversation runs. "closing" answers it while the closing cue of an End
// tap plays: the ticket and the send were out when End was tapped (the
// ticket's abort not honoured, as when its body was already arriving), the
// beat is the one that goes on under the cue, and the grant request is one
// an earlier conversation gave up on.
// stdout: [{phase, reason, playing, speaking, spoke, released, holds,
// notSent, unknown}]: `playing` whether speech played just before the
// answer, `speaking` whether it plays after it, `spoke` how many utterances
// started after it, `released` the grants the page released, `holds`
// whether a conversation or its cue still holds the page, `notSent` when the
// reason shown says "not sent: <reason>" (after End's own reason when
// closing, or after the lost pairing on a 401), `unknown` when it says
// delivery is unknown.
"use strict";
const { G } = require("./grant_ids.js");
const { REASON, LIMITS, withDetail } = require("../../llm_switchboard/desk/static/conversation_logic.js");
const { PATH } = require("../../llm_switchboard/desk/static/conversation.js");
const { DEST, flush, snap, harness, listening, releases } = require("./controller_harness.js");

async function drafted(h) {
  await listening(h);
  await h.hear("x");
  await h.hear("send it");
}

// A grant request given up on at its step deadline, let go when the hub's
// epoch changed, and the next conversation's grant held: the old request's
// call, still open.
async function abandonedGrant(h) {
  h.ctl.start(DEST);
  await flush();
  await h.answer(PATH.STATE, 200, snap());
  await h.advance(LIMITS.STEP_MS);
  h.ctl.snapshot(snap({ epoch: "e2" }));
  h.ctl.start(Object.assign({}, DEST, { epoch: "e2" }));
  await flush();
  await h.answer(PATH.STATE, 200, snap({ epoch: "e2" }));
  const [old, mine] = h.pending(PATH.GRANT);
  mine.answer(200, { grant: G.g1, expires_in: 15 });
  await flush();
  return old;
}

// For each path and mode: bring the page to the call, and return the call to answer.
const REACH = {
  ticket: {
    running: async (h) => { await drafted(h); return h.pending(PATH.TICKET)[0]; },
    closing: async (h) => {
      h.honorAbort = false;
      await drafted(h);
      h.ctl.endTap();
      return h.pending(PATH.TICKET)[0];
    }
  },
  send: {
    running: async (h) => {
      await drafted(h);
      await h.answer(PATH.TICKET, 200, { ticket: "t1" });
      return h.pending(PATH.SEND)[0];
    },
    closing: async (h) => {
      await drafted(h);
      await h.answer(PATH.TICKET, 200, { ticket: "t1" });
      h.ctl.endTap();
      return h.pending(PATH.SEND)[0];
    }
  },
  grant: {
    running: async (h) => {
      h.ctl.start(DEST);
      await flush();
      await h.answer(PATH.STATE, 200, snap());
      return h.pending(PATH.GRANT)[0];
    },
    closing: async (h) => {
      const old = await abandonedGrant(h);
      h.ctl.endTap();
      return old;
    }
  },
  beat: {
    running: async (h) => { await listening(h); await h.advance(LIMITS.BEAT_MS); return h.pending(PATH.BEAT)[0]; },
    closing: async (h) => {
      await listening(h);
      h.ctl.endTap();
      await h.advance(LIMITS.BEAT_MS);
      return h.pending(PATH.BEAT)[0];
    }
  }
};

async function probe(c) {
  const h = harness();
  const call = await REACH[c.path][c.mode](h);
  if (!call) throw new Error("no " + c.path + " call to answer in " + c.mode);
  const said = h.said.length;
  const playing = h.synth.speaking;
  call.answer(c.status, { error: c.reason });
  await flush();
  const v = h.view();
  // The end's own cause, which the send's answer follows: End, or a 401
  // that ended the conversation before its answer was read.
  const cause = c.mode === "closing" ? REASON.END_TAPPED + "; " : c.status === 401 ? REASON.UNPAIRED + "; " : "";
  return {
    phase: v.phase, reason: v.reason, playing: playing, speaking: h.synth.speaking, spoke: h.said.length - said,
    released: releases(h), holds: h.ctl.holds(),
    notSent: v.reason === cause + withDetail(REASON.NOT_SENT, c.reason), unknown: v.reason === REASON.UNKNOWN
  };
}

let input = "";
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", async () => {
  const out = [];
  for (const c of JSON.parse(input)) out.push(await probe(c));
  process.stdout.write(JSON.stringify(out));
});
