// Every call the tablet page can make, for the page tests that go through
// all of them (page_harness.js, the real app.js wiring): how to have each
// open while a closing cue plays and while a conversation runs.
"use strict";
const { G } = require("./grant_ids.js");
const {
  PATH, POLL, settle, snap, target, voiceSend, listen, cue, over, drafted, abandonedSnapshot, abandonedGrant,
  tapTicket, tapRefresh
} = require("./page_harness.js");
const { LIMITS } = require("./page_harness.js").L;

// For every call the page can make, how to have it open while a closing
// cue plays (`closing`) and while a conversation runs (`running`), as the
// call to answer; `starting` when the running conversation holds no grant
// yet. Two calls are left out because neither can be open then: the
// pairing calls are made only while unpaired, which ends any conversation
// and its cue first, and Start waits for a tapped send until it settles
// (the "Send" control is off while a conversation or its cue holds the page).
const CALLS = {
  "the poll": {
    running: async (h) => { await listen(h); return h.pending(POLL)[0]; },
    closing: async (h) => { await listen(h); await cue(h); return h.pending(POLL)[0]; }
  },
  "the Start snapshot": {
    starting: true,
    running: async (h) => { h.$("conv-start").click(); await settle(); return h.pending(PATH.STATE)[0]; }
  },
  "an abandoned Start snapshot": {
    running: async (h) => { const s = await abandonedSnapshot(h); await listen(h); return s; },
    closing: async (h) => { const s = await abandonedSnapshot(h); await listen(h); await cue(h); return s; }
  },
  "the grant request": {
    starting: true,
    running: async (h) => {
      h.$("conv-start").click();
      await settle();
      await h.answer(PATH.STATE, 200, snap());
      return h.pending(PATH.GRANT)[0];
    }
  },
  "an abandoned grant request": {
    running: async (h) => { const g = await abandonedGrant(h); await listen(h, G.g2, snap({ epoch: "e2" })); return g; },
    closing: async (h) => {
      const g = await abandonedGrant(h);
      await listen(h, G.g2, snap({ epoch: "e2" }));
      await cue(h);
      return g;
    }
  },
  "the beat": {
    running: async (h) => { await listen(h); await h.advance(LIMITS.BEAT_MS); return h.pending(PATH.BEAT)[0]; },
    closing: async (h) => { await listen(h); await cue(h); await h.advance(LIMITS.BEAT_MS); return h.pending(PATH.BEAT)[0]; }
  },
  "a beat past its abort": {
    running: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await h.advance(LIMITS.BEAT_MS + LIMITS.BEAT_ABORT_MS);
      return h.pending(PATH.BEAT)[0];
    },
    closing: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await h.advance(LIMITS.BEAT_MS + LIMITS.BEAT_ABORT_MS - 500);
      await cue(h);
      await h.advance(500);
      return h.pending(PATH.BEAT)[0];
    }
  },
  "an earlier conversation's grant release": {
    running: async (h) => { await listen(h); await over(h); await listen(h, G.g2); return h.pending(PATH.RELEASE)[0]; },
    closing: async (h) => {
      await listen(h);
      await over(h);
      await listen(h, G.g2);
      await cue(h);
      return h.pending(PATH.RELEASE)[0];
    }
  },
  "the voice ticket": {
    running: async (h) => { await listen(h); await drafted(h); return h.pending(PATH.TICKET)[0]; }
  },
  "an abandoned voice ticket": {
    running: async (h) => {
      h.honorAbort = false;
      await listen(h);
      await drafted(h);
      await over(h);
      await listen(h, G.g2);
      return h.pending(PATH.TICKET)[0];
    },
    closing: async (h) => { h.honorAbort = false; await listen(h); await drafted(h); await cue(h); return h.pending(PATH.TICKET)[0]; }
  },
  "the voice send": {
    running: async (h) => { await listen(h); await voiceSend(h, "check the logs"); return h.pending(PATH.SEND)[0]; },
    closing: async (h) => { await listen(h); await voiceSend(h, "check the logs"); await cue(h); return h.pending(PATH.SEND)[0]; }
  },
  "an earlier conversation's voice send": {
    running: async (h) => {
      await listen(h);
      await voiceSend(h, "check the logs");
      await over(h);
      await listen(h, G.g2);
      return h.pending(PATH.SEND)[0];
    }
  },
  "the tap flow's ticket": {
    running: async (h) => { tapTicket(h); await listen(h); return h.pending("/desk/api/ticket")[0]; },
    closing: async (h) => { tapTicket(h); await listen(h); await cue(h); return h.pending("/desk/api/ticket")[0]; }
  },
  "the tap flow's state refresh": {
    running: async (h) => { const r = await tapRefresh(h); await listen(h); return r; },
    closing: async (h) => { const r = await tapRefresh(h); await listen(h); await cue(h); return r; }
  },
  "the startup listing": {
    running: async (h) => { await listen(h); return h.pending("/desk/api/startup")[0]; },
    closing: async (h) => { await listen(h); await cue(h); return h.pending("/desk/api/startup")[0]; }
  },
  "a startup acknowledgement": {
    running: async (h) => { await ack(h); await listen(h); return h.pending("/desk/api/startup/ack")[0]; },
    closing: async (h) => { await ack(h); await listen(h); await cue(h); return h.pending("/desk/api/startup/ack")[0]; }
  },
  "a lock clear": {
    running: async (h) => { await clearLock(h); await listen(h); return h.pending("/desk/api/clear")[0]; },
    closing: async (h) => { await clearLock(h); await listen(h); await cue(h); return h.pending("/desk/api/clear")[0]; }
  },
  "an unenroll": {
    running: async (h) => { await listen(h); unenroll(h); return h.pending("/desk/api/unenroll")[0]; },
    closing: async (h) => { await listen(h); await cue(h); unenroll(h); return h.pending("/desk/api/unenroll")[0]; }
  }
};

async function ack(h) {
  await h.answer("/desk/api/startup", 200, { sessions: [{ session_id: "s1", digest: "d1", capture: "warning",
    refusal: null }] });
  h.$("startup-list").all((k) => k.getAttribute("data-testid") === "startup-ack")[0].click();
  await settle();
}

async function clearLock(h) {
  await h.answer(POLL, 200, snap({ targets: [target("s1", { lock: { lock_id: "k1", request_id: "r0", since: 1 } })] }));
  h.$("clear-btn").click();
  h.$("clear-yes").click();
  await settle();
  await h.answer(POLL, 200, snap());
}

function unenroll(h) {
  h.$("enroll-list").all((k) => k.getAttribute("data-testid") === "unenroll-btn")[0].click();
}

module.exports = { CALLS };
