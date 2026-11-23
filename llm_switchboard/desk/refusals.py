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

Every member also states its `Ownership` class, which the conversation page
acts on (docs/voice-desk.md, "The closing cue after a refusal"); a member
without one fails at import.
"""

from enum import Enum, StrEnum


class Ownership(Enum):
    """What a refusal of a ticket or a send shows about the page's sound slot.

    INTACT: the hub gives this refusal only after checking, in the same call,
    that the ticket's epoch and the target's one registration at the frozen
    generation still hold, and on a send that the conversation grant is
    current and names that target. The page may then speak its closing cue.
    UNPROVEN: anything else, including every refusal the hub can give before
    those checks on some path. The page ends silently: another page may own
    the slot now.
    """

    INTACT = "intact"
    UNPROVEN = "unproven"


class Refusal(StrEnum):
    def __new__(cls, reason: str, ownership: Ownership):
        member = str.__new__(cls, reason)
        member._value_ = reason
        member.ownership = ownership
        return member

    # -- every request -----------------------------------------------------
    WRONG_HOST = "wrong host", Ownership.UNPROVEN
    WRONG_ORIGIN = "wrong origin", Ownership.UNPROVEN
    NOT_FOUND = "not found", Ownership.UNPROVEN
    BAD_REQUEST = "bad request", Ownership.UNPROVEN
    METHOD_NOT_ALLOWED = "method not allowed", Ownership.UNPROVEN
    NOT_JSON = "content type must be application/json", Ownership.UNPROVEN
    LENGTH_REQUIRED = "length required", Ownership.UNPROVEN
    BODY_TOO_LARGE = "body too large", Ownership.UNPROVEN
    TRUNCATED_BODY = "truncated body", Ownership.UNPROVEN
    BAD_JSON = "bad json", Ownership.UNPROVEN
    INTERNAL_ERROR = "internal error", Ownership.UNPROVEN
    UNAUTHORIZED = "unauthorized", Ownership.UNPROVEN
    BAD_AFTER = "bad after", Ownership.UNPROVEN

    # -- tickets and sends -------------------------------------------------
    BAD_SESSION_ID = "bad session_id", Ownership.UNPROVEN
    BAD_GENERATION_OR_EPOCH = "bad generation or epoch", Ownership.UNPROVEN
    STALE_EPOCH = "stale epoch", Ownership.UNPROVEN
    STALE_GENERATION = "stale generation", Ownership.UNPROVEN
    NOT_REGISTERED = "not registered", Ownership.UNPROVEN
    AMBIGUOUS = "ambiguous", Ownership.UNPROVEN
    NOT_ENROLLED = "not enrolled", Ownership.UNPROVEN
    # Raised by begin_send and hand_off before the registration is looked at.
    SENDS_OFF = "sends off", Ownership.UNPROVEN
    LOCK_FILE_FULL = "lock file full", Ownership.UNPROVEN
    # issue_ticket: after the epoch, routing, single registration and
    # generation checks; begin_send: after those and the grant check.
    BUSY = "busy", Ownership.INTACT
    # issue_ticket only, after its busy check.
    TICKET_TABLE_FULL = "ticket table full", Ownership.INTACT
    BAD_TICKET = "bad ticket", Ownership.UNPROVEN
    UNKNOWN_TICKET = "unknown ticket", Ownership.UNPROVEN
    TICKET_EXPIRED = "ticket expired", Ownership.UNPROVEN
    TICKET_ALREADY_USED = "ticket already used", Ownership.UNPROVEN
    # begin_send only, after its busy check.
    TOO_MANY_OUTSTANDING = "too many outstanding requests", Ownership.INTACT
    REQUEST_NOT_PENDING = "request not pending", Ownership.UNPROVEN
    TARGET_CHANGED_NOT_SENT = "target changed; not sent", Ownership.UNPROVEN
    CHANNEL_IDENTITY_UNUSABLE = "channel identity unusable; not sent", Ownership.UNPROVEN

    # -- the text rule (text_rule.py; static/text_rule.js mirrors it) -------
    # begin_send checks the text before the ticket, the grant and the registration.
    EMPTY = "empty", Ownership.UNPROVEN
    TOO_LONG = "too long", Ownership.UNPROVEN
    INVALID_CHARACTER = "invalid character", Ownership.UNPROVEN
    OUTER_WHITESPACE = "leading or trailing whitespace", Ownership.UNPROVEN
    CONTROL_CHARACTER = "control character", Ownership.UNPROVEN
    COMMAND_CHARACTER = "starts with a command character", Ownership.UNPROVEN

    # -- conversation grants (grants.py) -------------------------------------
    BAD_CONFIRM = "bad confirm", Ownership.UNPROVEN
    # begin_send checks the voice gate before the grant and the registration.
    VOICE_OFF = "voice confirm off", Ownership.UNPROVEN
    GRANT_NOT_CURRENT = "conversation grant not current", Ownership.UNPROVEN
    GRANT_WRONG_TARGET = "conversation grant not for this target", Ownership.UNPROVEN
    # After the grant check, but before the registration is looked at.
    VOICE_TEXT_TOO_LONG = "too long for a voice send", Ownership.UNPROVEN
    ANOTHER_PAGE = "another page is paired; revoke its token first", Ownership.UNPROVEN
    TARGET_CHANGED = "target changed", Ownership.UNPROVEN
    CONVERSATION_LIVE = "a conversation is in progress", Ownership.UNPROVEN

    # -- channels, locks and clears ------------------------------------------
    TOO_MANY_REGISTRATIONS = "too many registrations", Ownership.UNPROVEN
    BAD_LOCK_REF = "bad session_id or lock_id", Ownership.UNPROVEN
    NO_LOCK = "no lock", Ownership.UNPROVEN
    LOCK_ID_MISMATCH = "lock id does not match", Ownership.UNPROVEN
    DELIVERY_IN_PROGRESS = "delivery in progress", Ownership.UNPROVEN
    CHANNEL_STILL_RUNNING = "the session's channel is still running; stop the session, then clear", Ownership.UNPROVEN
    CHANNEL_UNVERIFIABLE = "the session's channel can't be checked; stop the session, then clear", Ownership.UNPROVEN

    # -- pairing and tokens --------------------------------------------------
    PAGE_OUT_OF_DATE = "page out of date; reload it", Ownership.UNPROVEN
    BAD_SECRET_HASH = "bad secret_hash", Ownership.UNPROVEN
    TOO_MANY_PAIRING_ATTEMPTS = "too many pairing attempts", Ownership.UNPROVEN
    TOO_MANY_PENDING_PAIRINGS = "too many pending pairings", Ownership.UNPROVEN
    BAD_CODE = "bad code", Ownership.UNPROVEN
    NO_SUCH_PAIRING = "no such pairing", Ownership.UNPROVEN
    BAD_PAIR_CLAIM = "bad pair_id or secret", Ownership.UNPROVEN
    WRONG_SECRET = "wrong secret", Ownership.UNPROVEN
    NOT_CONFIRMED = "not confirmed", Ownership.UNPROVEN

    # -- the operator socket -------------------------------------------------
    UNKNOWN_OP = "unknown op", Ownership.UNPROVEN
    BAD_TOKEN_ID = "bad token_id", Ownership.UNPROVEN
    REVOKE_IN_PROGRESS = "revoked for new requests; an acknowledgement is still in progress", Ownership.UNPROVEN
    BAD_ENABLED = "enabled must be true or false", Ownership.UNPROVEN

# The refusals after which the conversation page may speak its closing cue;
# static/conversation_logic.js's OWNERSHIP_KEPT must equal it.
OWNERSHIP_KEPT = frozenset(r for r in Refusal if r.ownership is Ownership.INTACT)
