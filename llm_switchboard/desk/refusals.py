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

A refusal the send route gives before it writes the event to the channel
states `Delivery.NOT_SENT`, which the conversation page reads to show "not
sent: <reason>"; every other member is `Delivery.UNPROVEN`, and a member
whose class is not a `Delivery` fails at import. No refusal lets the page
speak: every end the hub brings to a conversation is silent
(docs/voice-desk.md, "The closing cue").
"""

from enum import Enum, StrEnum


class Delivery(Enum):
    """What a refusal of a send (`POST /desk/api/send`, hub.py's `Hub.send`)
    shows about the prompt.

    NOT_SENT: the send route gives this refusal only before the event is
    written to the channel (the HTTP layer's checks, `begin_send`, the
    dispatch recheck and `hand_off`), so nothing was handed off. UNPROVEN:
    anything else, including a refusal that could follow a write that may
    have reached the channel, one that says nothing about an earlier attempt,
    and every refusal the send route never gives.
    """

    NOT_SENT = "not sent"
    UNPROVEN = "unproven"


_NOT_SENT = Delivery.NOT_SENT


class Refusal(StrEnum):
    def __new__(cls, reason: str, delivery: Delivery = Delivery.UNPROVEN):
        if not isinstance(delivery, Delivery):
            raise TypeError(f"{reason!r}: {delivery!r} is not a Delivery")
        member = str.__new__(cls, reason)
        member._value_ = reason
        member.delivery = delivery
        return member

    # -- every request -----------------------------------------------------
    # On the send route the HTTP layer's checks run before Hub.send is called.
    WRONG_HOST = "wrong host", _NOT_SENT
    WRONG_ORIGIN = "wrong origin", _NOT_SENT
    NOT_FOUND = "not found"
    BAD_REQUEST = "bad request", _NOT_SENT
    METHOD_NOT_ALLOWED = "method not allowed", _NOT_SENT
    NOT_JSON = "content type must be application/json", _NOT_SENT
    LENGTH_REQUIRED = "length required", _NOT_SENT
    BODY_TOO_LARGE = "body too large", _NOT_SENT
    TRUNCATED_BODY = "truncated body", _NOT_SENT
    BAD_JSON = "bad json", _NOT_SENT
    # An unexpected exception, which can come after the channel write.
    INTERNAL_ERROR = "internal error"
    UNAUTHORIZED = "unauthorized", _NOT_SENT
    BAD_AFTER = "bad after"

    # -- tickets and sends -------------------------------------------------
    BAD_SESSION_ID = "bad session_id"
    BAD_GENERATION_OR_EPOCH = "bad generation or epoch"
    STALE_EPOCH = "stale epoch", _NOT_SENT
    STALE_GENERATION = "stale generation", _NOT_SENT
    NOT_REGISTERED = "not registered", _NOT_SENT
    AMBIGUOUS = "ambiguous", _NOT_SENT
    NOT_ENROLLED = "not enrolled", _NOT_SENT
    SENDS_OFF = "sends off", _NOT_SENT
    LOCK_FILE_FULL = "lock file full", _NOT_SENT
    BUSY = "busy", _NOT_SENT
    TICKET_TABLE_FULL = "ticket table full"
    BAD_TICKET = "bad ticket", _NOT_SENT
    UNKNOWN_TICKET = "unknown ticket", _NOT_SENT
    TICKET_EXPIRED = "ticket expired", _NOT_SENT
    # Before this attempt's write, but an earlier send with the same ticket
    # may have been handed off.
    TICKET_ALREADY_USED = "ticket already used"
    TOO_MANY_OUTSTANDING = "too many outstanding requests", _NOT_SENT
    REQUEST_NOT_PENDING = "request not pending", _NOT_SENT
    TARGET_CHANGED_NOT_SENT = "target changed; not sent", _NOT_SENT
    CHANNEL_IDENTITY_UNUSABLE = "channel identity unusable; not sent", _NOT_SENT

    # -- the text rule (text_rule.py; static/text_rule.js mirrors it) -------
    EMPTY = "empty", _NOT_SENT
    TOO_LONG = "too long", _NOT_SENT
    INVALID_CHARACTER = "invalid character", _NOT_SENT
    OUTER_WHITESPACE = "leading or trailing whitespace", _NOT_SENT
    CONTROL_CHARACTER = "control character", _NOT_SENT
    COMMAND_CHARACTER = "starts with a command character", _NOT_SENT

    # -- conversation grants (grants.py) -------------------------------------
    BAD_CONFIRM = "bad confirm", _NOT_SENT
    VOICE_OFF = "voice confirm off", _NOT_SENT
    GRANT_NOT_CURRENT = "conversation grant not current", _NOT_SENT
    GRANT_WRONG_TARGET = "conversation grant not for this target", _NOT_SENT
    VOICE_TEXT_TOO_LONG = "too long for a voice send", _NOT_SENT
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

    # -- the Mac link (link_hub.py) ------------------------------------------
    # Given before any handoff, but declared unproven so the page's NOT_SENT
    # list (static/conversation_answers.js) needs no change in this stage.
    LINK_BUSY = "desk link busy; try again"
    LINK_OPERATOR_BUSY = "busy: a Mac link call is in progress"
    BAD_HOST = "bad host"
    # A routing refusal before any handoff (see LINK_BUSY on why unproven).
    ENROLLED_ON_TWO_HOSTS = "enrolled on two hosts"
    CLEAR_STOPPED_NEEDS_CONFIRM = "confirm must repeat the lock id"
    CLEAR_STOPPED_PI_LOCK = "this lock is on the Pi; use desk clear"
    CLEAR_STOPPED_LINK_UP = ("the Mac that holds this lock is connected with its key; use desk clear, which "
                             "asks it whether the channel still runs")
    MAC_NOT_CONNECTED = ("the Mac that holds this lock is not connected, so its channel can't be checked; "
                         "reconnect that Mac, or, once you have seen the session exit there, use desk "
                         "clear-stopped on the Pi")

    # -- enrollment edits (remote_enrollment.py, hub_enrollment.py) -----------
    ENROLLMENT_FILES_UNREADABLE = "enrollment files unreadable; nothing changed"
    ENROLLED_ON_ANOTHER_HOST = "enrolled on another host"
    LOCKS_UNREADABLE = "locks.json can't be checked; nothing changed"
    HOST_ENROLLMENT_FULL = "Mac enrollment full"
    USE_ENROLL_HOST = "use enroll_host"

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

# The send refusals that prove nothing was handed off;
# static/conversation_answers.js's NOT_SENT must equal it.
NOT_SENT = frozenset(r for r in Refusal if r.delivery is Delivery.NOT_SENT)
