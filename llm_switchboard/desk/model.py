"""The hub's records, vocabularies and limits (docs/voice-desk.md, "Hub state").

Pure data: no I/O, clock or randomness. Every bound the hub enforces is a
named constant here so the state machine, the adapters and the tests agree.
"""

from dataclasses import dataclass
from enum import Enum
import re

# Phase 1 has one host; the Mac relay adds another in phase 2.
HOST = "pi"
KNOWN_HOSTS = frozenset({HOST})

EPOCH_BYTES = 16

# Tickets: one-use, bound to (epoch, session_id, generation).
TICKET_TTL_SECONDS = 10 * 60
MAX_TICKETS = 32
TICKET_RANDOM_BYTES = 18

# Requests kept in memory (and listed by the state API).
MAX_REQUESTS = 20
REQUEST_ID_RANDOM_BYTES = 8
# Answered, expired and unknown requests (and their reply text) are dropped
# this long after they finished; an unresolved lock is never aged out.
MAX_REQUEST_AGE_SECONDS = 24 * 3600

# Replies: the last answered replies, bounded by count and by text bytes.
MAX_REPLIES = 20
MAX_REPLY_RING_BYTES = 256 * 1024

# Live channel connections (verified or ambiguous) and persisted locks.
MAX_REGISTRATIONS = 64
MAX_LOCKS = 64
MAX_ENROLLED = 64
LOCK_ID_BYTES = 8
# A full file of MAX_LOCKS entries is about 20 KiB; anything larger is not ours.
MAX_LOCK_FILE_BYTES = 64 * 1024

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
# Transcript labels: only this much of a transcript's head and tail is read,
# and only when the file's identity (inode, size, mtime) changed.
LABEL_HEAD_BYTES = 64 * 1024
LABEL_TAIL_BYTES = 256 * 1024
MAX_LABEL_CACHE = 256

# Startup acknowledgement (one Enter at the development-channel warning):
# acknowledgements and blocked-session views per minute across all callers
# (the page polls every 5 s), and each tmux call's timeout, short because an
# acknowledgement holds the enrollment lock across its tmux calls.
MAX_STARTUP_ACKS_PER_MINUTE = 6
MAX_STARTUP_VIEWS_PER_MINUTE = 60
STARTUP_TMUX_TIMEOUT_SECONDS = 3
# How long a revoke waits for an acknowledgement already authorized with
# that token to finish: it is in flight only across the final capture and the
# guarded send, two tmux calls, so this bound is reached only if their
# timeouts fail. It stays under the operator socket's 10 s, so `desk revoke`
# gets its answer.
STARTUP_REVOKE_WAIT_SECONDS = 2 * STARTUP_TMUX_TIMEOUT_SECONDS + 2
# Views that already got their Enter, remembered for this hub run (pruned
# once their claude process is gone; this is the memory backstop).
MAX_STARTUP_ATTEMPTS = 1024

# Operator socket: concurrent connections and the request line's size.
MAX_OPERATOR_CONNECTIONS = 4
MAX_OPERATOR_LINE_BYTES = 4 * 1024

# Hub-issued ids: "<epoch hex>.<random hex>" and lock ids, as written in locks.json.
REQUEST_ID_PATTERN = re.compile(rf"[0-9a-f]{{{2 * EPOCH_BYTES}}}\.[0-9a-f]{{{2 * REQUEST_ID_RANDOM_BYTES}}}")
LOCK_ID_PATTERN = re.compile(rf"[0-9a-f]{{{2 * LOCK_ID_BYTES}}}")
SHA256_HEX_PATTERN = re.compile(r"[0-9a-f]{64}")
# A channel process's start time: /proc/<pid>/stat field 22 (clock ticks since boot).
CHANNEL_START_PATTERN = re.compile(r"[0-9]{1,20}")
MAX_PID = 2**31 - 1


def valid_channel_identity(pid, start) -> bool:
    """The (pid, start time) a lock records for the channel it was handed to."""
    return (isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid <= MAX_PID
            and isinstance(start, str) and CHANNEL_START_PATTERN.fullmatch(start) is not None)


class ChannelProcess(str, Enum):
    """What the process tree says about the channel a lock's event was handed to."""
    # No process with that pid, or one with another start time (the pid was reused).
    GONE = "gone"
    RUNNING = "running"
    # The process entry exists but its start time can't be read.
    UNVERIFIABLE = "unverifiable"


def is_request_id(value) -> bool:
    return isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value) is not None


class RequestState(str, Enum):
    PENDING = "pending"
    HANDED_OFF = "handed_off"
    ANSWERED = "answered"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


# States a request can still leave; the others are final.
OUTSTANDING_STATES = frozenset({RequestState.PENDING, RequestState.HANDED_OFF})


class RegistrationState(str, Enum):
    # A desk reply was received on this connection at some point. Historical:
    # the hub can't see Claude's current model or readiness.
    REPLIED = "replied"
    REGISTERED = "registered"
    NONE = "none"
    AMBIGUOUS = "ambiguous"


class LockStoreError(Exception):
    """The lock file can't be trusted or written; args[0] is a short reason,
    shown as the hub's lock_store_error. Every send is refused while it holds."""


class DeskError(Exception):
    """A refusal with the HTTP status it maps to; `reason` goes back as
    `{"error": reason}` and into logs (never prompt or reply text)."""

    def __init__(self, status: int, reason: str, fields: dict | None = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        # Machine-readable extras sent beside `error` (ids and states, never text).
        self.fields = dict(fields or {})


@dataclass
class Registration:
    conn_id: int
    session_id: str
    generation: int
    # The peer.PeerIdentity the channel was verified as; rechecked every 5 s.
    identity: object
    # A request handed to this connection was answered through `reply`.
    replied: bool = False


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
    # The bearer token that sent it, rechecked at handoff (revocation wins).
    token_hash: str = ""
    state: RequestState = RequestState.PENDING
    lock_id: str | None = None
    # When it left the outstanding states; history ages out from here.
    finished: float | None = None


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
    """One persisted target lock; never holds prompt or reply text. The
    channel's pid and start time say which process might still write the
    event to Claude, after a hub restart too."""

    lock_id: str
    host: str
    session_id: str
    request_id: str
    ticket_hash: str
    since: float
    channel_pid: int
    channel_start: str

    def to_json(self) -> dict:
        return {"lock_id": self.lock_id, "host": self.host, "session_id": self.session_id,
                "request_id": self.request_id, "ticket_hash": self.ticket_hash, "since": self.since,
                "channel_pid": self.channel_pid, "channel_start": self.channel_start}


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
