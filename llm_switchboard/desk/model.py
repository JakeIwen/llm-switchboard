"""The hub's records, vocabularies and limits (docs/voice-desk.md, "Hub state").

Pure data: no I/O, clock or randomness. Every bound the hub enforces is a
named constant here so the state machine, the adapters and the tests agree.
"""

from dataclasses import dataclass
from enum import Enum

# Phase 1 has one host; the Mac relay adds another in phase 2.
HOST = "pi"

EPOCH_BYTES = 16

# Tickets: one-use, bound to (epoch, session_id, generation).
TICKET_TTL_SECONDS = 10 * 60
MAX_TICKETS = 32
TICKET_RANDOM_BYTES = 18

# Requests kept in memory (and listed by the state API).
MAX_REQUESTS = 20
REQUEST_ID_RANDOM_BYTES = 8

# Replies: the last answered replies, bounded by count and by text bytes.
MAX_REPLIES = 20
MAX_REPLY_RING_BYTES = 256 * 1024

# Live channel connections (verified or ambiguous) and persisted locks.
MAX_REGISTRATIONS = 64
MAX_LOCKS = 64
MAX_ENROLLED = 64
LOCK_ID_BYTES = 8

# Pairing.
PAIR_TTL_SECONDS = 5 * 60
MAX_PENDING_PAIRINGS = 3
MAX_PAIR_STARTS_PER_MINUTE = 10
MAX_PAIR_SECRET_FAILURES = 5
PAIR_CODE_LENGTH = 6
PAIR_ID_BYTES = 16
MAX_PAIR_SECRET_LENGTH = 256
# Crockford base32: no I, L, O or U, so a code read aloud is unambiguous.
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Tokens.
TOKEN_BYTES = 32
TOKEN_TTL_SECONDS = 7 * 24 * 3600
MAX_TOKENS = 4
TOKEN_ID_LENGTH = 8

# Runtime cadence and HTTP bounds.
RECHECK_SECONDS = 5
LONG_POLL_SECONDS = 25
MAX_BODY_BYTES = 16 * 1024
MAX_HTTP_CONNECTIONS = 16
HTTP_REQUEST_TIMEOUT_SECONDS = 10
# Unverified channel connections still waiting for their hello.
MAX_PENDING_CHANNEL_CONNECTIONS = 8
HELLO_TIMEOUT_SECONDS = 10
# Cached transcript labels are reread at most this often per session.
LABEL_TTL_SECONDS = 60


class RequestState(str, Enum):
    PENDING = "pending"
    HANDED_OFF = "handed_off"
    ANSWERED = "answered"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


# States a request can still leave; the others are final.
OUTSTANDING_STATES = frozenset({RequestState.PENDING, RequestState.HANDED_OFF})


class RegistrationState(str, Enum):
    VERIFIED = "verified"
    REGISTERED = "registered"
    NONE = "none"
    AMBIGUOUS = "ambiguous"


class LockStoreError(Exception):
    """The lock file can't be trusted or written; args[0] is a short reason,
    shown as the hub's lock_store_error. Every send is refused while it holds."""


class DeskError(Exception):
    """A refusal with the HTTP status it maps to; `reason` goes back as
    `{"error": reason}` and into logs (never prompt or reply text)."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class Registration:
    conn_id: int
    session_id: str
    generation: int
    # The peer.PeerIdentity the channel was verified as; rechecked every 5 s.
    identity: object
    verified: bool = False


@dataclass
class Ticket:
    ticket_hash: str
    session_id: str
    generation: int
    expires_at: float
    consumed: bool = False


@dataclass
class Request:
    request_id: str
    session_id: str
    generation: int
    conn_id: int
    text: str
    created: float
    ticket_hash: str
    state: RequestState = RequestState.PENDING
    lock_id: str | None = None


@dataclass(frozen=True)
class Reply:
    request_id: str
    text: str
    spoken: str

    @property
    def size(self) -> int:
        return len(self.text.encode()) + len(self.spoken.encode())


@dataclass(frozen=True)
class Lock:
    """One persisted target lock; never holds prompt or reply text."""

    lock_id: str
    host: str
    session_id: str
    request_id: str
    ticket_hash: str
    since: float

    def to_json(self) -> dict:
        return {"lock_id": self.lock_id, "host": self.host, "session_id": self.session_id,
                "request_id": self.request_id, "ticket_hash": self.ticket_hash, "since": self.since}


@dataclass
class Pairing:
    pair_id: str
    code: str
    secret_hash: str
    expires_at: float
    confirmed: bool = False
    failures: int = 0


@dataclass(frozen=True)
class Token:
    token_id: str
    token_hash: str
    created: float
    expires_at: float
