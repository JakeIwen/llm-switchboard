"""The hub's refusals, declared once (docs/voice-desk.md, "Refusals").

Every reason the HTTPS API and the operator socket answer `{"error": reason}`
with, from hub.py, hub_state.py, grants.py and http_server.py, and every
reason the text rule (text_rule.py) gives, is a `Refusal`. The only other
reasons those modules send are free text from a store (the lock file's
messages from locks.py), passed through as they come.
tests/test_desk_refusals.py holds those modules to that, so a new refusal
can't reach a caller without being declared here. Members are `str`, so a
refusal compares, encodes and logs as its reason.

The startup acknowledgement has its own vocabulary (`StartupRefusal`), and
enrollment.py's refusals are those of the enroll routes.
"""

from enum import StrEnum


class Refusal(StrEnum):
    # -- every request -----------------------------------------------------
    WRONG_HOST = "wrong host"
    WRONG_ORIGIN = "wrong origin"
    NOT_FOUND = "not found"
    BAD_REQUEST = "bad request"
    METHOD_NOT_ALLOWED = "method not allowed"
    NOT_JSON = "content type must be application/json"
    LENGTH_REQUIRED = "length required"
    BODY_TOO_LARGE = "body too large"
    TRUNCATED_BODY = "truncated body"
    BAD_JSON = "bad json"
    INTERNAL_ERROR = "internal error"
    UNAUTHORIZED = "unauthorized"
    BAD_AFTER = "bad after"

    # -- tickets and sends -------------------------------------------------
    BAD_SESSION_ID = "bad session_id"
    BAD_GENERATION_OR_EPOCH = "bad generation or epoch"
    STALE_EPOCH = "stale epoch"
    STALE_GENERATION = "stale generation"
    NOT_REGISTERED = "not registered"
    AMBIGUOUS = "ambiguous"
    NOT_ENROLLED = "not enrolled"
    SENDS_OFF = "sends off"
    LOCK_FILE_FULL = "lock file full"
    BUSY = "busy"
    TICKET_TABLE_FULL = "ticket table full"
    BAD_TICKET = "bad ticket"
    UNKNOWN_TICKET = "unknown ticket"
    TICKET_EXPIRED = "ticket expired"
    TICKET_ALREADY_USED = "ticket already used"
    TOO_MANY_OUTSTANDING = "too many outstanding requests"
    REQUEST_NOT_PENDING = "request not pending"
    TARGET_CHANGED_NOT_SENT = "target changed; not sent"
    CHANNEL_IDENTITY_UNUSABLE = "channel identity unusable; not sent"

    # -- the text rule (text_rule.py; static/text_rule.js mirrors it) -------
    EMPTY = "empty"
    TOO_LONG = "too long"
    INVALID_CHARACTER = "invalid character"
    OUTER_WHITESPACE = "leading or trailing whitespace"
    CONTROL_CHARACTER = "control character"
    COMMAND_CHARACTER = "starts with a command character"

    # -- conversation grants (grants.py) -------------------------------------
    BAD_CONFIRM = "bad confirm"
    VOICE_OFF = "voice confirm off"
    GRANT_NOT_CURRENT = "conversation grant not current"
    GRANT_WRONG_TARGET = "conversation grant not for this target"
    VOICE_TEXT_TOO_LONG = "too long for a voice send"
    ANOTHER_PAGE = "another page is paired; revoke its token first"
    TARGET_CHANGED = "target changed"
    CONVERSATION_LIVE = "a conversation is in progress"

    # -- channels, locks and clears ------------------------------------------
    TOO_MANY_REGISTRATIONS = "too many registrations"
    BAD_LOCK_REF = "bad session_id or lock_id"
    NO_LOCK = "no lock"
    LOCK_ID_MISMATCH = "lock id does not match"
    DELIVERY_IN_PROGRESS = "delivery in progress"
    CHANNEL_STILL_RUNNING = "the session's channel is still running; stop the session, then clear"
    CHANNEL_UNVERIFIABLE = "the session's channel can't be checked; stop the session, then clear"

    # -- pairing and tokens --------------------------------------------------
    PAGE_OUT_OF_DATE = "page out of date; reload it"
    BAD_SECRET_HASH = "bad secret_hash"
    TOO_MANY_PAIRING_ATTEMPTS = "too many pairing attempts"
    TOO_MANY_PENDING_PAIRINGS = "too many pending pairings"
    BAD_CODE = "bad code"
    NO_SUCH_PAIRING = "no such pairing"
    BAD_PAIR_CLAIM = "bad pair_id or secret"
    WRONG_SECRET = "wrong secret"
    NOT_CONFIRMED = "not confirmed"

    # -- the operator socket -------------------------------------------------
    UNKNOWN_OP = "unknown op"
    BAD_TOKEN_ID = "bad token_id"
    REVOKE_IN_PROGRESS = "revoked for new requests; an acknowledgement is still in progress"
    BAD_ENABLED = "enabled must be true or false"
