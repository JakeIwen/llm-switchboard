// Conversation mode's vocabulary (docs/voice-desk.md, "Conversation
// mode"): its phases, the hub's values it mirrors, limits, timers, effects,
// events, commands, cues, end reasons and Start's blockers, declared once.
// conversation_logic.js, the reducer, re-exports every name, which is how
// the controller, the view and the tests reach them. Pure, so node can load it.
(function (root) {
  "use strict";

  // `listening` is the only phase with an open mic.
  var PHASE = Object.freeze({
    OFF: "off", STARTING: "starting", LISTENING: "listening", READING: "reading",
    SENDING: "sending", WAITING: "waiting", SPEAKING: "speaking", ENDED: "ended"
  });

  // Mirrors model.ConfirmMode, model.RequestState and model.RegistrationState
  // (tests/test_desk_conversation_js.py fails if they drift).
  var CONFIRM = Object.freeze({ TAP: "tap", VOICE: "voice" });
  var REQUEST_STATE = Object.freeze({
    PENDING: "pending", HANDED_OFF: "handed_off", ANSWERED: "answered", UNKNOWN: "unknown", EXPIRED: "expired"
  });
  var REGISTRATION = Object.freeze({ REPLIED: "replied", REGISTERED: "registered", NONE: "none", AMBIGUOUS: "ambiguous" });

  var LIMITS = Object.freeze({
    // After any desk sound ends, no recognizer starts sooner than this. The
    // tablet spike heard no leak with 1,500 ms; 500 ms is the plan's figure.
    // One constant for every mic opening.
    GUARD_MS: 500,
    // A start() that brings no audiostart in this time ends the conversation:
    // the restart gap measured 160 to 220 ms to audiostart, so this is a hang.
    WATCHDOG_MS: 5000,
    IDLE_MS: 3 * 60 * 1000,
    TURN_MS: 5 * 60 * 1000,
    ACK_WAIT_MS: 30 * 1000,
    REPLY_WAIT_MS: 10 * 60 * 1000,
    QUICK_END_MS: 1000,
    QUICK_ENDS: 3,
    BEAT_MS: 5000,
    BEAT_ABORT_MS: 4000,
    // The page gives up 10 s after the start of its last good beat, before
    // the hub's 15 s grant expiry (grants.GRANT_TTL_SECONDS), so the page
    // never acts on a grant the hub has already dropped.
    FRESH_MS: 10000,
    // How often the controller checks freshness between other events.
    TICK_MS: 1000,
    // A start step (the snapshot, the wake lock, the grant) or a ticket that
    // hasn't answered in this time fails; the send POST has the tap flow's deadline.
    STEP_MS: 10000,
    SEND_DEADLINE_MS: 20000,
    // Mirrors model.MAX_VOICE_TEXT_BYTES.
    MAX_DRAFT_BYTES: 1024
  });

  // The grant id grants.py issues, GRANT_ID_BYTES random bytes in lowercase
  // hex: an answer naming anything else holds no grant.
  var GRANT_ID = /^[0-9a-f]{32}$/;

  // An utterance gets twice the time estimated from its text before it counts
  // as failed; the same estimate sets the quiet period after a cut-off one.
  var SPEECH_DEADLINE_FACTOR = 2;

  // Every timer the conversation runs, the reducer's and the controller's,
  // so one name can't mean two timers.
  var TIMER = Object.freeze({
    GUARD: "guard", WATCHDOG: "watchdog", IDLE: "idle", TURN: "turn", ACK: "ack", REPLY: "reply",
    BEAT: "beat", BEAT_ABORT: "beat_abort", TICK: "tick", SPEECH: "speech", STEP: "step"
  });

  // What an utterance is for; the end of the conversation's own last cue is
  // the controller's (END), never the reducer's.
  var SAY = Object.freeze({ CUE: "cue", READBACK: "readback", REPLY: "reply", SENT: "sent", END: "end" });

  var FX = Object.freeze({
    LISTEN: "listen", STOP_LISTEN: "stopListen", SAY: "say", INTERIM: "interim",
    TIMER: "timer", CLEAR_TIMER: "clearTimer", SNAPSHOT: "snapshot", WAKE_LOCK: "wakeLock",
    GRANT: "grant", TICKET: "ticket", SEND: "send", END: "end"
  });

  var EV = Object.freeze({
    START: "start", SNAPSHOT_FETCHED: "snapshot_fetched", WAKE_OK: "wake_ok", GRANT_OK: "grant_ok",
    FAIL: "fail", END: "end", SNAPSHOT: "snapshot", TICK: "tick", BEAT_OK: "beat_ok",
    RESULT: "result", REC_AUDIOSTART: "rec_audiostart", REC_END: "rec_end", REC_ERROR: "rec_error",
    REC_START_FAILED: "rec_start_failed", SAY_END: "say_end", SAY_FAILED: "say_failed",
    TIMEOUT: "timeout", TICKET_OK: "ticket_ok", TICKET_FAIL: "ticket_fail",
    SEND_OK: "send_ok", SEND_REFUSED: "send_refused", SEND_UNKNOWN: "send_unknown",
    // A status other than 200 from the hub, on any call the page makes.
    HUB_STATUS: "hub_status"
  });

  // The phrases the reducer recognizes as commands (conversation_logic.js).
  var COMMAND = Object.freeze({ SEND: "send", SCRATCH: "scratch", READBACK: "readback", STOP: "stop" });

  var CUE = Object.freeze({
    SENT: "Sent.",
    NOTHING_TO_SEND: "Nothing to send.",
    CLEARED: "Cleared.",
    EMPTY_DRAFT: "The draft is empty.",
    OVERFLOW: "Too long. The draft was cleared.",
    CANT_SEND: "Can't send that",
    NO_SPOKEN: "No spoken summary.",
    ENDED: "Conversation ended"
  });

  // Why a conversation ended: spoken as "Conversation ended: <reason>." when
  // the page may still speak, else only shown. A reason with a detail is
  // `withDetail(REASON.X, detail)`.
  var REASON = Object.freeze({
    STOPPED: "you said stop listening",
    END_TAPPED: "End was tapped",
    HIDDEN: "the page was hidden",
    EXITED: "the page was closed or frozen",
    RETARGET: "another session was selected",
    UNPAIRED: "this tablet is no longer paired",
    WAKE: "the screen wake lock was released",
    NO_WAKE: "the screen can't be kept awake",
    NO_VOICE: "this tablet has no voice",
    IDLE: "nothing was heard for three minutes",
    TURN: "the turn took over five minutes",
    HUB_LOST: "the hub stopped answering",
    GRANT_LOST: "the hub ended the conversation",
    REFUSED: "the hub refused the conversation",
    HUB_UNREACHABLE: "the hub could not be reached",
    // Any status but 200, at its status line, shown with it ("HTTP 409")
    // until that call's own answer, if its body arrives, says more.
    CALL_FAILED: "a hub call failed",
    // A 200 that is not its call's success, or whose body was lost, for a
    // call the conversation had already given up on or one of the page's own.
    UNUSABLE: "a hub answer could not be used",
    REC_FAILED: "speech recognition failed",
    REC_HUNG: "speech recognition did not start",
    REC_LOOP: "speech recognition keeps stopping",
    // A late end or error of the page's own speech: the mic may have heard it.
    ECHO: "the desk may have heard its own speech",
    SPEECH: "the voice failed",
    EPOCH: "the hub restarted",
    TARGET: "the session changed or went away",
    SENDS_OFF: "sends are off",
    VOICE_OFF: "voice confirm is off",
    LOCK_STORE: "the hub's lock store failed",
    BUSY: "the session is busy",
    FOREIGN: "another request reached the session",
    NOT_SENT: "not sent",
    UNKNOWN: "delivery is unknown; check the session",
    // Said after an end's cause once the send it left open was accepted.
    HANDED_OFF: "the prompt was handed off; check the session for its reply",
    NO_REPLY: "the request ended without a reply",
    REPLY_TIMEOUT: "no reply came in ten minutes"
  });

  // Why Start conversation is refused right now, shown under the control.
  // The reducer checks the hub's facts again once a conversation starts.
  var BLOCKER = Object.freeze({
    NO_STATE: "the hub has not answered yet",
    SENDS_OFF: REASON.SENDS_OFF,
    VOICE_OFF: REASON.VOICE_OFF,
    LOCK_STORE: REASON.LOCK_STORE,
    UNAVAILABLE: "the session is not available for sends",
    BUSY: REASON.BUSY,
    NO_RECOGNIZER: "this tablet has no speech recognition",
    NO_VOICE: REASON.NO_VOICE,
    TAP_SEND: "a tapped send is still in progress",
    NOT_QUIET: "the desk may still be speaking; wait until it is quiet",
    IN_PROGRESS: "a conversation is in progress",
    LETTING_GO: "the hub has not answered the last conversation's grant request; reload the page if it never does"
  });

  function withDetail(reason, detail) { return detail ? reason + ": " + detail : reason; }

  var api = {
    PHASE: PHASE, CONFIRM: CONFIRM, REQUEST_STATE: REQUEST_STATE, REGISTRATION: REGISTRATION,
    LIMITS: LIMITS, GRANT_ID: GRANT_ID, SPEECH_DEADLINE_FACTOR: SPEECH_DEADLINE_FACTOR, TIMER: TIMER,
    SAY: SAY, FX: FX, EV: EV, COMMAND: COMMAND, CUE: CUE, REASON: REASON, BLOCKER: BLOCKER, withDetail: withDetail
  };
  root.DeskConversationVocabulary = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
