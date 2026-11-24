"""The desk link's framing (docs/voice-desk.md, "Framing"), shared by the
hub (Pi), the pump (Pi) and the relay (Mac).

One JSON object per line, UTF-8, `\\n`-terminated, at most
MAX_LINK_LINE_BYTES. `encode_frame` and `decode_frame` share one table of
field validators with exact key sets, so a sender can't build a frame its
receiver refuses. The first frame each way (`link_hello`,
`link_challenge`) and `link_refused` have shapes that never change between
versions and carry no `v`; every other frame carries `"v": <version>`.
Every `reason` (and presence `state`) is a wire code from LINK_CODES, never
free text. Lines are parsed only after their length and nesting are
checked (`shallow_json`). Standard library only.
"""

from enum import StrEnum
import json
import re

from ..host_protocol import HOST_PATTERN
from ..session_id import is_session_id
from ..session_names import MAX_TITLE_LEN
from .channel import UndeliverableReason
from .model import EPOCH_BYTES, HOST, ChannelProcess, ReplyRefusal, is_request_id, valid_channel_identity
from .shallow_json import loads_shallow

LINK_VERSIONS = (1,)
# The channel protocol's 64 KiB line plus room for the link fields, so any
# channel frame the relay accepts fits once rewrapped.
MAX_LINK_LINE_BYTES = 80 * 1024
# Frames are flat objects; `versions` is the one list, so nothing nests deeper.
MAX_FRAME_DEPTH = 2
MAX_VERSIONS = 4
# chan, query_id and versions live in 1..2^31-1; per-link counters (ping n,
# generation) are bounded by what JSON carries exactly.
MAX_COUNTER = 2**31 - 1
MAX_EXACT_INT = 2**53
MAX_CWD_LENGTH = 4096
MAX_SIGNATURE_BYTES = 4 * 1024
MACHINE_HEX_LENGTH = 32
NONCE_BYTES = 32
LINK_ID_BYTES = 16

# -- timing (seconds) -----------------------------------------------------------
HEARTBEAT_SECONDS = 15
IDLE_TIMEOUT_SECONDS = 45
ADMISSION_DEADLINE_SECONDS = 10
HANDSHAKE_DEADLINE_SECONDS = 30
FRAME_DEADLINE_SECONDS = 5
SEND_GUARD_SECONDS = 6
PRESENCE_DEADLINE_SECONDS = 5
REGISTER_WAIT_SECONDS = 3.5
BACKOFF_START_SECONDS = 1
BACKOFF_MAX_SECONDS = 60
BACKOFF_JITTER = 0.2
BACKOFF_RESET_AFTER_SECONDS = 60
HUB_NOT_RUNNING_BACKOFF_MAX_SECONDS = 5 * 60
FIXED_BACKOFF_SECONDS = 10 * 60
REFUSAL_CACHE_SECONDS = 25

# -- caps -----------------------------------------------------------------------
LINK_QUEUE_FRAMES = 64
LINK_QUEUE_BYTES = 1024 * 1024
CHANNEL_QUEUE_FRAMES = 8
MAX_RELAY_CHANNELS = 16
MAX_RELAY_UNVERIFIED_CHANNELS = 8
RELAY_REFUSAL_CACHE_ENTRIES = 32
RELAY_REMEMBERED_REQUESTS = 8
MAX_LINK_REGISTRATIONS = 16
MAX_PRESENCE_QUERIES = 4
MAX_REGISTERS_PER_MINUTE = 120
MAX_PENDING_ADMISSIONS = 2
MAX_ACCEPTS_PER_MINUTE = 20
MAX_ADMISSIONS_PER_HOST_PER_MINUTE = 6
MAX_LINK_HOSTS = 4
MAX_LINK_HTTP_WAITS = 4
MAX_LINK_OPERATOR_WAITS = 1


class LinkFrameError(ValueError):
    """A frame that can't be sent or must close the link. args[0] is a
    short fixed reason, never the frame's content."""


class Side(StrEnum):
    HUB = "hub"
    RELAY = "relay"


class LinkType(StrEnum):
    LINK_HELLO = "link_hello"
    LINK_CHALLENGE = "link_challenge"
    LINK_REFUSED = "link_refused"
    LINK_PROOF = "link_proof"
    LINK_WELCOME = "link_welcome"
    REGISTER = "register"
    REGISTERED = "registered"
    REGISTER_REFUSED = "register_refused"
    UNREGISTERED = "unregistered"
    DROP = "drop"
    EVENT = "event"
    DELIVERED = "delivered"
    UNDELIVERABLE = "undeliverable"
    REPLY = "reply"
    REPLY_RESULT = "reply_result"
    PRESENCE_QUERY = "presence_query"
    PRESENCE = "presence"
    PING = "ping"
    PONG = "pong"


# The shapes that never change between versions and carry no `v`.
VERSION_FREE = frozenset({LinkType.LINK_HELLO, LinkType.LINK_CHALLENGE, LinkType.LINK_REFUSED})


# -- wire codes -------------------------------------------------------------------

class RefusedCode(StrEnum):
    VERSION = "version"
    HUB_NOT_RUNNING = "hub_not_running"
    TOO_MANY_RECONNECTS = "too_many_reconnects"
    TOO_MANY_HOSTS = "too_many_hosts"
    NOT_AUTHORIZED = "not_authorized"
    PROOF_FAILED = "proof_failed"
    SIGNERS_UNREADABLE = "signers_unreadable"
    VERIFIER_UNAVAILABLE = "verifier_unavailable"
    ADMISSION_TIMEOUT = "admission_timeout"


class RegisterRefusedCode(StrEnum):
    NOT_ENROLLED = "not_enrolled"
    ENROLLED_ON_ANOTHER_HOST = "enrolled_on_another_host"
    TOO_MANY_REGISTRATIONS = "too_many_registrations"
    TOO_MANY_REGISTERS = "too_many_registers"


class UnregisteredCode(StrEnum):
    CHANNEL_CLOSED = "channel_closed"
    CHANNEL_IDLE = "channel_idle"
    CHANNEL_WRITE_FAILED = "channel_write_failed"
    CHANNEL_BUSY = "channel_busy"
    RELAY_RECHECK_FAILED = "relay_recheck_failed"


class DropCode(StrEnum):
    UNENROLLED = "unenrolled"
    HUB_RECHECK_FAILED = "hub_recheck_failed"


class UndeliverableCode(StrEnum):
    # The channel's own report, classified from channel.UndeliverableReason.
    GUARD_SESSION_FILE = "guard_session_file"
    GUARD_SESSION_CHANGED = "guard_session_changed"
    STDOUT_FAILED = "stdout_failed"
    CHANNEL_OTHER = "channel_other"
    # The relay's own: it handed no byte of the event to the channel.
    CHANNEL_CLOSED = "channel_closed"
    CHANNEL_BUSY = "channel_busy"
    CHANNEL_NOT_WRITTEN = "channel_not_written"
    RELAY_RECHECK_FAILED = "relay_recheck_failed"


class ReplyResultCode(StrEnum):
    BAD_REPLY = "bad_reply"
    NOT_UTF8 = "not_utf8"
    TOO_LARGE = "too_large"
    UNKNOWN_REQUEST = "unknown_request"
    WRONG_CONNECTION = "wrong_connection"
    NOT_OUTSTANDING = "not_outstanding"
    LOCK_STORE_ERROR = "lock_store_error"
    SESSION_CHANGED = "session_changed"


class PresenceState(StrEnum):
    GONE = "gone"
    RUNNING = "running"
    UNVERIFIABLE = "unverifiable"


# The one table: the codes each message type's `reason` (presence: `state`) may carry.
LINK_CODES: dict[LinkType, frozenset[str]] = {
    LinkType.LINK_REFUSED: frozenset(RefusedCode),
    LinkType.REGISTER_REFUSED: frozenset(RegisterRefusedCode),
    LinkType.UNREGISTERED: frozenset(UnregisteredCode),
    LinkType.DROP: frozenset(DropCode),
    LinkType.UNDELIVERABLE: frozenset(UndeliverableCode),
    LinkType.REPLY_RESULT: frozenset(ReplyResultCode),
    LinkType.PRESENCE: frozenset(PresenceState),
}
# The section gives codes as `[a-z_]{1,40}`, but its own table has `not_utf8`:
# the table wins, so a digit is allowed after the first letter.
_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,39}")

# Keyed maps over the hub's and the channel's own vocabularies, so a reason
# without a code fails a test, and the relay turns a code back into the
# exact text a Pi channel would get.
REPLY_REFUSAL_CODES: dict[ReplyRefusal, ReplyResultCode] = {
    ReplyRefusal.BAD_REPLY: ReplyResultCode.BAD_REPLY,
    ReplyRefusal.NOT_UTF8: ReplyResultCode.NOT_UTF8,
    ReplyRefusal.TOO_LARGE: ReplyResultCode.TOO_LARGE,
    ReplyRefusal.UNKNOWN_REQUEST: ReplyResultCode.UNKNOWN_REQUEST,
    ReplyRefusal.WRONG_CONNECTION: ReplyResultCode.WRONG_CONNECTION,
    ReplyRefusal.NOT_OUTSTANDING: ReplyResultCode.NOT_OUTSTANDING,
    ReplyRefusal.LOCK_STORE_ERROR: ReplyResultCode.LOCK_STORE_ERROR,
    ReplyRefusal.SESSION_CHANGED: ReplyResultCode.SESSION_CHANGED,
}
REPLY_REFUSAL_TEXT: dict[ReplyResultCode, ReplyRefusal] = {code: refusal for refusal, code in
                                                           REPLY_REFUSAL_CODES.items()}
UNDELIVERABLE_REASON_CODES: dict[UndeliverableReason, UndeliverableCode] = {
    UndeliverableReason.SESSION_FILE: UndeliverableCode.GUARD_SESSION_FILE,
    UndeliverableReason.SESSION_CHANGED: UndeliverableCode.GUARD_SESSION_CHANGED,
    UndeliverableReason.STDOUT_CLOSED: UndeliverableCode.STDOUT_FAILED,
}
PRESENCE_CODES: dict[ChannelProcess, PresenceState] = {
    ChannelProcess.GONE: PresenceState.GONE,
    ChannelProcess.RUNNING: PresenceState.RUNNING,
    ChannelProcess.UNVERIFIABLE: PresenceState.UNVERIFIABLE,
}
PRESENCE_PROCESS: dict[PresenceState, ChannelProcess] = {code: process for process, code in
                                                         PRESENCE_CODES.items()}


def undeliverable_code(reason) -> UndeliverableCode:
    """The wire code for a channel's own undeliverable report; anything the
    channel says that isn't one of its known reasons is `channel_other`."""
    try:
        return UNDELIVERABLE_REASON_CODES.get(UndeliverableReason(reason), UndeliverableCode.CHANNEL_OTHER)
    except ValueError:
        return UndeliverableCode.CHANNEL_OTHER


# -- field validators -------------------------------------------------------------

_HOST = re.compile(HOST_PATTERN)
_LINK_ID = re.compile(rf"[0-9a-f]{{{2 * LINK_ID_BYTES}}}")
_MACHINE = re.compile(rf"[0-9a-f]{{{MACHINE_HEX_LENGTH}}}")
_EPOCH = re.compile(rf"[0-9a-f]{{{2 * EPOCH_BYTES}}}")
_NONCE = re.compile(rf"[0-9a-f]{{{2 * NONCE_BYTES}}}")
_RELAY_BUILD = re.compile(r"[0-9a-f]{0,40}")
_SIGNATURE = re.compile(r"-----BEGIN SSH SIGNATURE-----\n(?:[A-Za-z0-9+/=]{1,76}\n){1,64}"
                        r"-----END SSH SIGNATURE-----\n")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def _int_in(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def valid_version(value) -> bool:
    return _int_in(value, 1, MAX_COUNTER)


def valid_versions(value) -> bool:
    """A version list as a hello or a refusal carries it: 1 to 4 distinct versions."""
    return (isinstance(value, list) and 1 <= len(value) <= MAX_VERSIONS
            and all(valid_version(v) for v in value) and len(set(value)) == len(value))


def valid_link_host(value) -> bool:
    """A Mac's host name: v1's host-name rule, and never the desk's own `pi`."""
    return isinstance(value, str) and _HOST.fullmatch(value) is not None and value != HOST


def valid_machine(value) -> bool:
    return isinstance(value, str) and _MACHINE.fullmatch(value) is not None


def valid_epoch(value) -> bool:
    return isinstance(value, str) and _EPOCH.fullmatch(value) is not None


def valid_nonce(value) -> bool:
    return isinstance(value, str) and _NONCE.fullmatch(value) is not None


def valid_signature_armor(value) -> bool:
    """An armored SSH signature as `ssh-keygen -Y sign` writes it, at most 4 KiB."""
    return (isinstance(value, str) and len(value) <= MAX_SIGNATURE_BYTES
            and _SIGNATURE.fullmatch(value) is not None)


def _counter(value) -> bool:
    return _int_in(value, 1, MAX_COUNTER)


def _label(value) -> bool:
    return isinstance(value, str) and len(value) <= MAX_TITLE_LEN and _CONTROL.search(value) is None


def _cwd(value) -> bool:
    return (isinstance(value, str) and len(value) <= MAX_CWD_LENGTH and _CONTROL.search(value) is None
            and (value == "" or value.startswith("/")))


def _start(value) -> bool:
    return valid_channel_identity(1, value)


def _pid(value) -> bool:
    return valid_channel_identity(value, "0")


_FIELDS = {
    "versions": valid_versions,
    "relay_build": lambda v: isinstance(v, str) and _RELAY_BUILD.fullmatch(v) is not None,
    "host": valid_link_host,
    "machine": valid_machine,
    "version": valid_version,
    "epoch": valid_epoch,
    "nonce": valid_nonce,
    "signature": valid_signature_armor,
    "link_id": lambda v: isinstance(v, str) and _LINK_ID.fullmatch(v) is not None,
    "heartbeat_seconds": lambda v: _int_in(v, 1, 3600),
    "idle_timeout_seconds": lambda v: _int_in(v, 1, 3600),
    "max_line_bytes": lambda v: _int_in(v, 1024, 16 * 1024 * 1024),
    "chan": _counter,
    "query_id": _counter,
    "generation": lambda v: _int_in(v, 0, MAX_EXACT_INT),
    "n": lambda v: _int_in(v, 1, MAX_EXACT_INT),
    "session_id": lambda v: isinstance(v, str) and is_session_id(v),
    "channel_pid": _pid,
    "claude_pid": _pid,
    "channel_start": _start,
    "claude_start": _start,
    "label": _label,
    "cwd": _cwd,
    "request_id": is_request_id,
    # Content and reply text are bounded by the line cap; the hub sizes
    # replies itself (ReplyRefusal.TOO_LARGE), so a big one is not a bad frame.
    "content": lambda v: isinstance(v, str),
    "text": lambda v: isinstance(v, str),
    "spoken": lambda v: isinstance(v, str),
    "ok": lambda v: isinstance(v, bool),
}

# Each type's fields (besides `type`, and `v` outside VERSION_FREE) and who sends it.
_SHAPES: dict[LinkType, tuple[frozenset[Side], tuple[str, ...]]] = {
    LinkType.LINK_HELLO: (frozenset({Side.RELAY}), ("versions", "relay_build", "host", "machine")),
    LinkType.LINK_CHALLENGE: (frozenset({Side.HUB}), ("version", "epoch", "nonce")),
    # Also written by the pump, which stands in for an absent hub.
    LinkType.LINK_REFUSED: (frozenset({Side.HUB}), ("reason", "versions")),
    LinkType.LINK_PROOF: (frozenset({Side.RELAY}), ("signature",)),
    LinkType.LINK_WELCOME: (frozenset({Side.HUB}),
                            ("link_id", "heartbeat_seconds", "idle_timeout_seconds", "max_line_bytes")),
    LinkType.REGISTER: (frozenset({Side.RELAY}), ("chan", "session_id", "channel_pid", "channel_start",
                                                  "claude_pid", "claude_start", "label", "cwd")),
    LinkType.REGISTERED: (frozenset({Side.HUB}), ("chan", "generation")),
    LinkType.REGISTER_REFUSED: (frozenset({Side.HUB}), ("chan", "reason")),
    LinkType.UNREGISTERED: (frozenset({Side.RELAY}), ("chan", "reason")),
    LinkType.DROP: (frozenset({Side.HUB}), ("chan", "reason")),
    LinkType.EVENT: (frozenset({Side.HUB}), ("chan", "request_id", "content")),
    LinkType.DELIVERED: (frozenset({Side.RELAY}), ("chan", "request_id")),
    LinkType.UNDELIVERABLE: (frozenset({Side.RELAY}), ("chan", "request_id", "reason")),
    LinkType.REPLY: (frozenset({Side.RELAY}), ("chan", "request_id", "text", "spoken")),
    LinkType.REPLY_RESULT: (frozenset({Side.HUB}), ("chan", "request_id", "ok", "reason")),
    LinkType.PRESENCE_QUERY: (frozenset({Side.HUB}), ("query_id", "channel_pid", "channel_start")),
    LinkType.PRESENCE: (frozenset({Side.RELAY}), ("query_id", "state")),
    LinkType.PING: (frozenset({Side.HUB, Side.RELAY}), ("n",)),
    LinkType.PONG: (frozenset({Side.HUB, Side.RELAY}), ("n",)),
}


# The fields whose values are wire codes, checked through LINK_CODES.
_CODE_FIELDS = frozenset({"reason", "state"})


def sender_of(kind: LinkType) -> frozenset[Side]:
    return _SHAPES[kind][0]


def _check_codes(kind: LinkType, fields: dict) -> None:
    codes = LINK_CODES.get(kind)
    if kind is LinkType.REPLY_RESULT:
        if fields["ok"]:
            if fields["reason"] is not None:
                raise LinkFrameError("reason on an accepted reply")
            return
    if codes is None:
        return
    key = "state" if kind is LinkType.PRESENCE else "reason"
    value = fields[key]
    if not isinstance(value, str) or _CODE_PATTERN.fullmatch(value) is None or value not in codes:
        raise LinkFrameError(f"code outside its table: {kind.value}")


def _check(kind: LinkType, fields: dict, sender: Side) -> None:
    senders, names = _SHAPES[kind]
    if sender not in senders:
        raise LinkFrameError(f"{kind.value} from the wrong side")
    if set(fields) != set(names):
        raise LinkFrameError(f"wrong fields: {kind.value}")
    for name in names:
        if name in _CODE_FIELDS:
            continue  # checked against LINK_CODES below
        if not _FIELDS[name](fields[name]):
            raise LinkFrameError(f"bad field: {kind.value}.{name}")
    _check_codes(kind, fields)


def encode_frame(kind: LinkType, sender: Side, *, v: int | None = None, **fields) -> bytes:
    """One line for `kind` from `sender`. `v` is required except for
    the version-free shapes, which never carry it. LinkFrameError when the
    frame would be refused by its receiver or is over the cap."""
    kind = LinkType(kind)
    _check(kind, fields, sender)
    head = {"type": kind.value}
    if kind not in VERSION_FREE:
        if not valid_version(v):
            raise LinkFrameError("no version for a versioned frame")
        head["v"] = v
    elif v is not None:
        raise LinkFrameError("a version-free frame carries no version")
    try:
        data = (json.dumps({**head, **fields}, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LinkFrameError("not UTF-8") from exc
    if len(data) > MAX_LINK_LINE_BYTES:
        raise LinkFrameError("oversize")
    return data


def decode_frame(line: bytes, sender: Side, *, v: int | None) -> tuple[LinkType, dict]:
    """The type and fields (without `type` and `v`) of one whole line from
    `sender`. `v` is the negotiated version, or None before the
    challenge, when only the version-free shapes can arrive. Any
    LinkFrameError closes the link."""
    if len(line) > MAX_LINK_LINE_BYTES:
        raise LinkFrameError("oversize")
    if not line.endswith(b"\n") or b"\n" in line[:-1]:
        raise LinkFrameError("not one whole line")
    try:
        message = loads_shallow(line[:-1], MAX_FRAME_DEPTH)
    except ValueError as exc:
        raise LinkFrameError("invalid JSON") from exc
    if not isinstance(message, dict):
        raise LinkFrameError("not an object")
    try:
        kind = LinkType(message.pop("type", None))
    except ValueError as exc:
        raise LinkFrameError("unknown type") from exc
    if kind in VERSION_FREE:
        if "v" in message:
            raise LinkFrameError(f"a version on {kind.value}")
    else:
        carried = message.pop("v", None)
        if v is None or type(carried) is not int or carried != v:
            raise LinkFrameError("wrong v")
    _check(kind, message, sender)
    return kind, message


class LineSplitter:
    """Whole lines out of a byte stream, each checked against the cap
    before it is complete: a partial line over the cap raises at once, and
    a partial line left at EOF is never returned."""

    def __init__(self, max_line_bytes: int = MAX_LINK_LINE_BYTES):
        self._max = max_line_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        lines = []
        start = 0
        while (end := self._buffer.find(b"\n", start)) >= 0:
            if end + 1 - start > self._max:
                raise LinkFrameError("oversize")
            lines.append(bytes(self._buffer[start:end + 1]))
            start = end + 1
        del self._buffer[:start]
        if len(self._buffer) >= self._max:  # no room left for its newline
            raise LinkFrameError("oversize")
        return lines

    @property
    def pending(self) -> int:
        """Bytes of an unfinished line held; dropped, never parsed, at EOF."""
        return len(self._buffer)
