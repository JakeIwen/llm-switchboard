"""The relay's channel socket on the Mac (docs/voice-desk.md, "Framing",
"Deadlines, heartbeats and backpressure", "Identity on the Mac").

Mac channel servers (the unchanged phase 1 `bin/desk-channel`) connect to
`DeskConfig.channel_socket` and speak the channel protocol (`protocol.py`)
to the relay as they would to the hub on the Pi. For each connection the
relay checks the socket peer's uid before it reads anything, reads one
bounded hello, verifies the channel through `peer_mac`, and forwards a
`register` over the current link (an `Uplink`, which `relay_link` provides).
After the hub's `registered` it writes the channel `welcome` and then:

- answers the channel's `ping` itself, pings a channel it hasn't written to
  for the heartbeat interval, and closes one silent for the idle timeout;
- rechecks the channel every 5 s and before every event and reply;
- writes to each channel through its own `LinkWriter` (8 frames, each with
  5 s of awake time from its submit) and reports each event's evidence to
  the hub by "Writers: one outcome per frame": `undeliverable` with one of
  the relay's own codes only for an event no byte of which reached the
  channel, never for one the writer began;
- refuses a channel's `reply` itself unless its request id is one of the
  last 8 the relay forwarded to that channel, so a typo never puts reply
  text on the link;
- refuses for 25 s, without a `register`, a session the hub just refused
  as not enrolled (the refusal cache).

The hub decides everything else. Nothing here logs or forwards free text:
logs carry wire codes, session id prefixes and request ids of the hub's
shape. The identity checks prevent accidents between honest processes; only
the uid check separates users ("Who the design defends against").
Standard library only.
"""

from collections import Counter, OrderedDict, deque
import contextlib
from enum import Enum, StrEnum
import logging
import os
import socket
import threading
import time
from typing import Callable, Protocol

from ..session_id import is_session_id
from ..session_names import MAX_TITLE_LEN
from . import peer_mac, protocol
from .config import DeskConfig
from .link_protocol import (
    CHANNEL_QUEUE_FRAMES, FRAME_DEADLINE_SECONDS, HEARTBEAT_SECONDS, IDLE_TIMEOUT_SECONDS, MAX_COUNTER,
    MAX_CWD_LENGTH, MAX_RELAY_CHANNELS, MAX_RELAY_UNVERIFIED_CHANNELS, REFUSAL_CACHE_SECONDS,
    REGISTER_WAIT_SECONDS, RELAY_REFUSAL_CACHE_ENTRIES, RELAY_REMEMBERED_REQUESTS, REPLY_REFUSAL_TEXT,
    LineSplitter, LinkFrameError, LinkType, RegisterRefusedCode, ReplyResultCode, UndeliverableCode,
    UnregisteredCode, undeliverable_code,
)
from .link_writer import Frame, LinkWriter, SocketTarget, SubmitRefusal
from .model import HELLO_TIMEOUT_SECONDS, RECHECK_SECONDS, ReplyRefusal, WriteOutcome, is_request_id
from .private_socket import bind_private_socket
from .protocol import MessageType
from .rate_log import RateLog
from .refusals import Refusal
from .session_identity import read_session_file

log = logging.getLogger("llm_switchboard.desk.relay")

# macOS `sun_path` holds 104 bytes with its terminating NUL.
SUN_PATH_MAX_BYTES = 103
_POLL_SECONDS = 0.5
_READ_BYTES = 65536
_REFUSAL_SEND_SECONDS = 1.0
# A writer stops within one poll slice of its retirement; this only bounds a bug.
_WRITER_JOIN_SECONDS = 2.0
_CONTROL = frozenset(chr(c) for c in [*range(0x20), *range(0x7f, 0xa0), 0x2028, 0x2029])


class RelayRefusal(StrEnum):
    """The text of every `refused` and local `reply_result` the relay gives
    a channel itself (the hub's own refusals arrive as codes and are turned
    back into the hub's text below)."""
    LINK_DOWN = "desk link down"
    LINK_VERSION = "desk link version mismatch"
    LINK_SLOW = "desk link slow"
    BUSY = "busy"
    TOO_MANY_CHANNELS = "too many desk channels on this Mac"
    BAD_HELLO = "bad hello"
    NO_SUCH_REQUEST = "no desk request with that id on this session; the reply was not delivered"
    REPLY_LINK_BUSY = "desk link busy; the reply was not delivered"
    # The channel's own wording for the same case (channel.HubLink.send_reply).
    REPLY_IN_FLIGHT = "a reply for this request is already in flight; wait for its result"


# The text a Pi channel gets from the hub for the same case: peer.py's
# not-enrolled refusal and the hub's registration cap. The other two codes
# have no Pi case, so they get words of the same kind.
REGISTER_REFUSAL_TEXT: dict[RegisterRefusedCode, str] = {
    RegisterRefusedCode.NOT_ENROLLED: "session is not enrolled",
    RegisterRefusedCode.ENROLLED_ON_ANOTHER_HOST: "session is enrolled on another host",
    RegisterRefusedCode.TOO_MANY_REGISTRATIONS: Refusal.TOO_MANY_REGISTRATIONS.value,
    RegisterRefusedCode.TOO_MANY_REGISTERS: "too many registrations; try again shortly",
}


class Uplink(Protocol):
    """The relay's side of one link as the channel socket uses it
    (`relay_link.RelayLink`). Every frame a channel causes goes to the link
    object the channel registered on, never to a successor."""

    epoch: str

    @property
    def retired(self) -> bool: ...

    def submit(self, kind: LinkType, **fields) -> bool:
        """Queue one frame to the hub; False when it couldn't be queued.
        LinkFrameError for a frame the hub would refuse."""

    def retire(self, reason: str) -> None: ...


class _Phase(Enum):
    PENDING = "pending"  # `register` sent; no answer yet
    LIVE = "live"        # registered and welcomed
    CLOSED = "closed"    # refused, timed out, or closing


class _Channel:
    """One verified channel connection, from its `register` to its close."""

    def __init__(self, chan: int, sock: socket.socket, identity: peer_mac.MacIdentity, uplink: Uplink,
                 splitter: LineSplitter, early: list[bytes], now: float):
        self.chan = chan
        self.sock = sock
        self.identity = identity
        self.uplink = uplink
        self.splitter = splitter
        # Lines that came with the hello (a channel sends nothing before welcome).
        self.early = early
        self.phase = _Phase.PENDING
        self.answered = threading.Event()
        self.refusal: str | None = None
        self.writer: LinkWriter | None = None
        # Events queued to the writer, in order, for the report at close.
        self.events: list[tuple[str, Frame]] = []
        self.recent: deque[str] = deque(maxlen=RELAY_REMEMBERED_REQUESTS)
        self.replies: set[str] = set()
        self.last_received = self.last_sent = self.last_recheck = now
        self.close_code: UnregisteredCode | None = None
        # The writer stopped by itself (a deadline or a failed write).
        self.writer_failed = False
        self.report = False


def _clean_label(value) -> str:
    """The session file's `name` as `register` carries it: one line, no
    control characters (else empty), cut to MAX_TITLE_LEN."""
    if not isinstance(value, str) or any(c in _CONTROL for c in value):
        return ""
    return value[:MAX_TITLE_LEN]


def _clean_cwd(value) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > MAX_CWD_LENGTH
            or any(c in _CONTROL for c in value)):
        return ""
    return value


def _utf8(*values: str) -> bool:
    try:
        for value in values:
            value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class RelayChannels:
    """The relay's listener and every channel connection on it. `reader`
    replaces `peer_mac`'s kernel reader (tests); `clock` is awake time, the
    clock the channel's own timers use, for the channel writers' deadlines,
    the idle rule and the refusal cache."""

    def __init__(self, config: DeskConfig, *, reader=None, clock: Callable[[], float] = time.monotonic,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS, idle_seconds: float = IDLE_TIMEOUT_SECONDS,
                 recheck_seconds: float = RECHECK_SECONDS, register_wait_seconds: float = REGISTER_WAIT_SECONDS,
                 write_deadline_seconds: float = FRAME_DEADLINE_SECONDS, target_factory=SocketTarget,
                 exit_process: Callable[[], None] | None = None):
        self.config = config
        self._reader = reader
        self._clock = clock
        self._heartbeat = heartbeat_seconds
        self._idle = idle_seconds
        self._recheck_every = recheck_seconds
        self._register_wait = register_wait_seconds
        self._write_deadline = write_deadline_seconds
        self._target_factory = target_factory
        self._exit_process = exit_process or (lambda: os._exit(1))
        self._rate_log = RateLog(clock)
        self._lock = threading.Lock()
        self._channels: dict[int, _Channel] = {}
        self._issued = 0
        self._unverified = 0
        self._verified = 0
        self._uplink: Uplink | None = None
        self._down = RelayRefusal.LINK_DOWN
        self._refused: OrderedDict[tuple[str, int], float] = OrderedDict()
        self._listener: socket.socket | None = None
        self._stopping = threading.Event()
        # Late and unsolicited hub messages, dropped quietly (counted, not logged).
        self.late = Counter()
        self._hub_handlers = {
            LinkType.REGISTERED: self._on_registered,
            LinkType.REGISTER_REFUSED: self._on_register_refused,
            LinkType.DROP: self._on_drop,
            LinkType.EVENT: self._on_event,
            LinkType.REPLY_RESULT: self._on_reply_result,
        }
        self._channel_handlers = {
            MessageType.PING: lambda ch, message: self._send_channel(ch, MessageType.PONG),
            MessageType.PONG: lambda ch, message: None,
            MessageType.DELIVERED: self._on_delivered,
            MessageType.UNDELIVERABLE: self._on_undeliverable,
            MessageType.REPLY: self._on_reply,
        }

    # -- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        path = self.config.channel_socket
        if len(os.fsencode(path)) > SUN_PATH_MAX_BYTES:
            raise ValueError(f"the channel socket path is over {SUN_PATH_MAX_BYTES} bytes")
        self._listener = bind_private_socket(path)
        self._listener.settimeout(_POLL_SECONDS)
        threading.Thread(target=self._accept, name="relay-accept", daemon=True).start()

    def close(self) -> None:
        self._stopping.set()
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._listener.close()
            with contextlib.suppress(OSError):
                os.unlink(self.config.channel_socket)
        with self._lock:
            channels = list(self._channels.values())
        for ch in channels:
            self._close(ch, None, report=False)

    # -- the link, as relay_link reports it ------------------------------------------------

    hub_frames = frozenset({LinkType.REGISTERED, LinkType.REGISTER_REFUSED, LinkType.DROP, LinkType.EVENT,
                            LinkType.REPLY_RESULT})

    def link_up(self, uplink: Uplink) -> None:
        with self._lock:
            self._uplink = uplink

    def link_down(self, uplink: Uplink | None = None, refusal: RelayRefusal = RelayRefusal.LINK_DOWN) -> None:
        """`uplink` is gone (None: whatever was up). Hellos are refused with
        `refusal` until `link_up`. Every channel on that link is closed and
        nothing is reported: the link is gone, and the hub has already made
        its requests unknown."""
        with self._lock:
            if uplink is None or self._uplink is uplink:
                self._uplink = None
            self._down = refusal
            doomed = [ch for ch in self._channels.values() if uplink is None or ch.uplink is uplink]
        for ch in doomed:
            self._close(ch, None, report=False)

    def handle_hub_frame(self, uplink: Uplink, kind: LinkType, fields: dict) -> None:
        """One decoded hub frame for a channel (`hub_frames`). LinkFrameError
        for a protocol violation, which closes the link."""
        self._hub_handlers[kind](uplink, fields)

    def registered_count(self) -> int:
        with self._lock:
            return sum(ch.phase is _Phase.LIVE for ch in self._channels.values())

    # -- accept and admission -----------------------------------------------------------------

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                sock, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                room = self._unverified < MAX_RELAY_UNVERIFIED_CHANNELS and self._verified < MAX_RELAY_CHANNELS
                if room:
                    self._unverified += 1
            if not room:
                if self._rate_log.due("accept_full"):
                    log.warning("desk relay: too many channel connections; closing one")
                sock.close()
                continue
            threading.Thread(target=self._serve, args=(sock,), name="relay-channel", daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        try:
            ch = self._admit(sock)
        except Exception:
            log.exception("desk relay: channel admission raised")
            ch = None
        finally:
            with self._lock:
                self._unverified -= 1
        if ch is None:
            sock.close()
            return
        try:
            if ch.phase is _Phase.LIVE:
                self._read(ch)
            else:
                self._refuse(sock, ch.refusal or self._down)
        except Exception:
            log.exception("desk relay: channel reader raised")
        finally:
            self._finish(ch)

    def _admit(self, sock: socket.socket) -> _Channel | None:
        """uid, hello, link state, the Mac check, the refusal cache, then
        `register` and the wait for the hub's answer. None when refused
        before a `chan` was issued (the refusal already sent)."""
        try:
            pid = peer_mac.socket_peer_pid(sock, reader=self._reader)
        except peer_mac.PeerRefused:
            return None  # another user, or no credentials: nothing is read or answered
        splitter = LineSplitter(protocol.MAX_LINE_BYTES)
        try:
            hello, *early = self._read_lines(sock, splitter, HELLO_TIMEOUT_SECONDS)
            kind, message = protocol.decode(hello)
        except (OSError, TimeoutError, protocol.FrameError, LinkFrameError):
            return None
        session_id = message.get("session_id")
        if kind is not MessageType.HELLO or not isinstance(session_id, str) or not is_session_id(session_id):
            return self._refuse(sock, RelayRefusal.BAD_HELLO)
        with self._lock:
            uplink, down = self._uplink, self._down
        if uplink is None or uplink.retired:
            return self._refuse(sock, down)
        try:
            identity = peer_mac.verify_peer(pid, session_id, self.config, reader=self._reader)
        except peer_mac.PeerRefused as exc:
            reason = str(exc.args[0] if exc.args else "refused")
            if self._rate_log.due(f"verify:{reason}"):
                log.info("desk relay: channel refused: %s", reason)
            return self._refuse(sock, reason)
        if self._recently_refused(identity):
            return self._refuse(sock, REGISTER_REFUSAL_TEXT[RegisterRefusedCode.NOT_ENROLLED])
        ch = self._new_channel(sock, identity, uplink, splitter, early)
        if ch is None:
            return self._refuse(sock, RelayRefusal.TOO_MANY_CHANNELS)
        self._register(ch)
        return ch

    def _new_channel(self, sock, identity, uplink, splitter, early) -> _Channel | None:
        with self._lock:
            if self._verified >= MAX_RELAY_CHANNELS:
                return None
            if self._issued >= MAX_COUNTER:
                # A chan never recurs, so the relay ends with its range.
                log.error("desk relay: channel numbers exhausted; exiting")
                self._exit_process()
                return None
            self._issued += 1
            ch = _Channel(self._issued, sock, identity, uplink, splitter, early, self._clock())
            self._channels[ch.chan] = ch
            self._verified += 1
        return ch

    def _register(self, ch: _Channel) -> None:
        entry = read_session_file(self.config.claude_sessions_dir, ch.identity.claude_pid) or {}
        identity = ch.identity
        try:
            queued = ch.uplink.submit(
                LinkType.REGISTER, chan=ch.chan, session_id=identity.session_id, channel_pid=identity.channel_pid,
                channel_start=identity.channel_start, claude_pid=identity.claude_pid,
                claude_start=identity.claude_start, label=_clean_label(entry.get("name")),
                cwd=_clean_cwd(entry.get("cwd")))
        except LinkFrameError:
            queued = False
        if not queued:
            self._settle_pending(ch, RelayRefusal.BUSY)
        elif not ch.answered.wait(self._register_wait):
            self._settle_pending(ch, RelayRefusal.LINK_SLOW)

    def _settle_pending(self, ch: _Channel, refusal: str) -> None:
        """Refuse a channel still waiting for the hub; one the hub has
        registered meanwhile stays registered."""
        with self._lock:
            if ch.phase is _Phase.PENDING:
                ch.phase = _Phase.CLOSED
                ch.refusal = refusal
        ch.answered.set()

    def _refuse(self, sock: socket.socket, reason: str) -> None:
        with contextlib.suppress(OSError, protocol.FrameError):
            sock.settimeout(_REFUSAL_SEND_SECONDS)
            sock.sendall(protocol.encode(MessageType.REFUSED, reason=str(reason)))
        return None

    @staticmethod
    def _read_lines(sock: socket.socket, splitter: LineSplitter, timeout: float) -> list[bytes]:
        """The first whole lines to arrive, within `timeout` (the hello and
        anything sent with it)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            data = sock.recv(_READ_BYTES)
            if not data:
                raise ConnectionResetError("channel closed")
            lines = splitter.feed(data)
            if lines:
                return lines

    # -- the refusal cache -------------------------------------------------------------------

    def _recently_refused(self, identity: peer_mac.MacIdentity) -> bool:
        key, now = (identity.session_id, identity.claude_pid), self._clock()
        with self._lock:
            expires = self._refused.get(key)
            if expires is None:
                return False
            if now >= expires:
                del self._refused[key]
                return False
            return True

    def _remember_refusal(self, identity: peer_mac.MacIdentity) -> None:
        key = (identity.session_id, identity.claude_pid)
        with self._lock:
            self._refused[key] = self._clock() + REFUSAL_CACHE_SECONDS
            self._refused.move_to_end(key)
            while len(self._refused) > RELAY_REFUSAL_CACHE_ENTRIES:
                self._refused.popitem(last=False)

    # -- frames from the hub --------------------------------------------------------------------

    def _lookup(self, uplink: Uplink, chan: int) -> _Channel | None:
        """The channel `chan` names on this link; None when it is closed (or
        belongs to another link). A `chan` the relay never issued closes the link."""
        with self._lock:
            if chan > self._issued:
                raise LinkFrameError("chan never issued")
            ch = self._channels.get(chan)
        return ch if ch is not None and ch.uplink is uplink else None

    def _on_registered(self, uplink: Uplink, fields: dict) -> None:
        chan = fields["chan"]
        ch = self._lookup(uplink, chan)
        with self._lock:
            if ch is not None and ch.phase is _Phase.LIVE:
                raise LinkFrameError("registered twice")
            live = ch is not None and ch.phase is _Phase.PENDING
            if live:
                ch.phase = _Phase.LIVE
                ch.report = True
                ch.writer = self._writer_for(ch)
        if not live:
            # The channel left or was refused for time: the hub drops it again.
            self.late["registered"] += 1
            self._report(uplink, LinkType.UNREGISTERED, chan=chan, reason=UnregisteredCode.CHANNEL_CLOSED)
            return
        ch.writer.start()
        self._send_channel(ch, MessageType.WELCOME, epoch=uplink.epoch, generation=fields["generation"])
        log.info("desk relay: registered session %s chan %d", ch.identity.session_id[:8], chan)
        ch.answered.set()

    def _writer_for(self, ch: _Channel) -> LinkWriter:
        return LinkWriter(self._target_factory(ch.sock), max_frames=CHANNEL_QUEUE_FRAMES,
                          deadline_seconds=self._write_deadline, clock=self._clock,
                          on_failure=lambda stop: self._writer_stopped(ch), name=f"relay-chan-{ch.chan}")

    def _writer_stopped(self, ch: _Channel) -> None:
        ch.writer_failed = True
        self._close(ch, UnregisteredCode.CHANNEL_WRITE_FAILED)

    def _on_register_refused(self, uplink: Uplink, fields: dict) -> None:
        ch = self._lookup(uplink, fields["chan"])
        if ch is None or ch.phase is _Phase.CLOSED:
            self.late["register_refused"] += 1
            return
        if ch.phase is _Phase.LIVE:
            raise LinkFrameError("register_refused for a registered chan")
        code = RegisterRefusedCode(fields["reason"])
        if code is RegisterRefusedCode.NOT_ENROLLED:
            self._remember_refusal(ch.identity)
        if self._rate_log.due(f"register_refused:{code.value}"):
            log.info("desk relay: register refused: %s", code.value)
        self._settle_pending(ch, REGISTER_REFUSAL_TEXT[code])

    def _on_drop(self, uplink: Uplink, fields: dict) -> None:
        ch = self._lookup(uplink, fields["chan"])
        if ch is None or ch.phase is _Phase.CLOSED:
            self.late["drop"] += 1
            return
        if ch.phase is _Phase.PENDING:
            raise LinkFrameError("drop for an unregistered chan")
        log.info("desk relay: hub dropped chan %d: %s", ch.chan, fields["reason"])
        # The hub has dropped the registration already; nothing it would act on is left to report.
        self._close(ch, None, report=False)

    def _on_event(self, uplink: Uplink, fields: dict) -> None:
        chan, request_id = fields["chan"], fields["request_id"]
        ch = self._lookup(uplink, chan)
        if ch is not None and ch.phase is _Phase.PENDING:
            raise LinkFrameError("event for an unregistered chan")
        if ch is None or ch.phase is not _Phase.LIVE:
            self.late["event"] += 1
            self._undeliverable(uplink, chan, request_id, UndeliverableCode.CHANNEL_CLOSED)
            return
        if self._recheck_failed(ch):
            self._undeliverable(uplink, chan, request_id, UndeliverableCode.RELAY_RECHECK_FAILED)
            self._close(ch, UnregisteredCode.RELAY_RECHECK_FAILED)
            return
        try:
            data = protocol.encode(MessageType.EVENT, request_id=request_id, content=fields["content"])
        except protocol.FrameError:
            self._undeliverable(uplink, chan, request_id, UndeliverableCode.CHANNEL_NOT_WRITTEN)
            return
        with self._lock:
            ch.recent.append(request_id)
            frame = ch.writer.submit(data)
            if frame.refusal is None:
                ch.events.append((request_id, frame))
                ch.last_sent = self._clock()
        if frame.refusal is SubmitRefusal.FULL:
            self._undeliverable(uplink, chan, request_id, UndeliverableCode.CHANNEL_BUSY)
            self._close(ch, UnregisteredCode.CHANNEL_BUSY)
        elif frame.refusal is SubmitRefusal.RETIRED:
            # Closing already: its close sends the `unregistered`.
            self._undeliverable(uplink, chan, request_id, UndeliverableCode.CHANNEL_CLOSED)
        else:
            log.info("desk relay: event %s queued to chan %d", request_id, chan)

    def _on_reply_result(self, uplink: Uplink, fields: dict) -> None:
        ch = self._lookup(uplink, fields["chan"])
        request_id = fields["request_id"]
        with self._lock:
            waiting = ch is not None and ch.phase is _Phase.LIVE and request_id in ch.replies
            if waiting:
                ch.replies.discard(request_id)
        if not waiting:
            self.late["reply_result"] += 1
            return
        reason = None if fields["ok"] else REPLY_REFUSAL_TEXT[ReplyResultCode(fields["reason"])].value
        self._send_channel(ch, MessageType.REPLY_RESULT, request_id=request_id, ok=fields["ok"], reason=reason)

    # -- the channel's own frames ----------------------------------------------------------------

    def _read(self, ch: _Channel) -> None:
        """The registered channel's reader: its frames, the heartbeat, the
        idle rule and the periodic recheck, until the channel closes."""
        ch.sock.settimeout(_POLL_SECONDS)
        try:
            for line in ch.early:
                self._channel_frame(ch, line)
            while ch.phase is _Phase.LIVE:
                if not self._tick(ch):
                    return
                try:
                    data = ch.sock.recv(_READ_BYTES)
                except TimeoutError:
                    continue
                except OSError:
                    data = b""
                if not data:
                    self._close(ch, UnregisteredCode.CHANNEL_CLOSED)
                    return
                ch.last_received = self._clock()
                for line in ch.splitter.feed(data):
                    self._channel_frame(ch, line)
        except (LinkFrameError, protocol.FrameError):
            self._close(ch, UnregisteredCode.CHANNEL_CLOSED)

    def _tick(self, ch: _Channel) -> bool:
        now = self._clock()
        if now - ch.last_received >= self._idle:
            log.info("desk relay: chan %d idle; closing", ch.chan)
            self._close(ch, UnregisteredCode.CHANNEL_IDLE)
            return False
        if now - ch.last_recheck >= self._recheck_every and self._recheck_failed(ch):
            self._close(ch, UnregisteredCode.RELAY_RECHECK_FAILED)
            return False
        if now - ch.last_sent >= self._heartbeat:
            self._send_channel(ch, MessageType.PING)
        return True

    def _channel_frame(self, ch: _Channel, line: bytes) -> None:
        kind, message = protocol.decode(line)
        handler = self._channel_handlers.get(kind)
        if handler is None:
            raise protocol.FrameError(f"unexpected {kind.value}")
        handler(ch, message)

    def _on_delivered(self, ch: _Channel, message: dict) -> None:
        request_id = message.get("request_id")
        if is_request_id(request_id):
            self._report(ch.uplink, LinkType.DELIVERED, chan=ch.chan, request_id=request_id)

    def _on_undeliverable(self, ch: _Channel, message: dict) -> None:
        request_id = message.get("request_id")
        if is_request_id(request_id):
            self._undeliverable(ch.uplink, ch.chan, request_id, undeliverable_code(message.get("reason")))

    def _on_reply(self, ch: _Channel, message: dict) -> None:
        request_id, text, spoken = message.get("request_id"), message.get("text"), message.get("spoken")
        with self._lock:
            known = isinstance(request_id, str) and is_request_id(request_id) and request_id in ch.recent
            in_flight = known and request_id in ch.replies
        if not known:
            return self._reply_refused(ch, request_id, RelayRefusal.NO_SUCH_REQUEST)
        if in_flight:
            return self._reply_refused(ch, request_id, RelayRefusal.REPLY_IN_FLIGHT)
        if not isinstance(text, str) or not isinstance(spoken, str):
            return self._reply_refused(ch, request_id, ReplyRefusal.BAD_REPLY)
        if not _utf8(text, spoken):
            return self._reply_refused(ch, request_id, ReplyRefusal.NOT_UTF8)
        if self._recheck_failed(ch):
            frame = self._reply_refused(ch, request_id, ReplyRefusal.SESSION_CHANGED)
            if frame is not None:
                frame.wait(self._write_deadline + 1)  # the refusal goes out before the close
            self._close(ch, UnregisteredCode.RELAY_RECHECK_FAILED)
            return None
        with self._lock:
            ch.replies.add(request_id)  # before the submit: the hub's answer may come at once
        try:
            queued = ch.uplink.submit(LinkType.REPLY, chan=ch.chan, request_id=request_id, text=text, spoken=spoken)
        except LinkFrameError:
            queued = False
        if not queued:
            with self._lock:
                ch.replies.discard(request_id)
            return self._reply_refused(ch, request_id, RelayRefusal.REPLY_LINK_BUSY)
        return None

    def _reply_refused(self, ch: _Channel, request_id, reason: str) -> Frame | None:
        return self._send_channel(ch, MessageType.REPLY_RESULT,
                                  request_id=request_id if isinstance(request_id, str) else None,
                                  ok=False, reason=str(reason))

    # -- writing, reporting and closing ------------------------------------------------------------

    def _send_channel(self, ch: _Channel, kind: MessageType, **fields) -> Frame | None:
        """Queue one frame (not an event) to the channel's writer. One that
        doesn't fit closes the channel `channel_busy`."""
        try:
            data = protocol.encode(kind, **fields)
        except (protocol.FrameError, UnicodeEncodeError):
            return None
        with self._lock:
            frame = ch.writer.submit(data)
            if frame.refusal is None:
                ch.last_sent = self._clock()
        if frame.refusal is SubmitRefusal.FULL:
            self._close(ch, UnregisteredCode.CHANNEL_BUSY)
        return frame if frame.refusal is None else None

    def _undeliverable(self, uplink: Uplink, chan: int, request_id: str, code: UndeliverableCode) -> None:
        if self._rate_log.due(f"undeliverable:{code.value}"):
            log.info("desk relay: event %s undeliverable: %s", request_id, code.value)
        self._report(uplink, LinkType.UNDELIVERABLE, chan=chan, request_id=request_id, reason=code)

    def _report(self, uplink: Uplink, kind: LinkType, **fields) -> None:
        """Evidence the hub's request table needs: losing it would lose that
        evidence, so a frame that can't be queued retires the link."""
        try:
            queued = uplink.submit(kind, **fields)
        except LinkFrameError:
            log.error("desk relay: %s not encodable", kind.value)
            queued = False
        if not queued:
            uplink.retire(f"{kind.value} not queued")

    def _recheck_failed(self, ch: _Channel) -> bool:
        ch.last_recheck = self._clock()
        reason = peer_mac.recheck(ch.identity, self.config, reader=self._reader)
        if reason is not None and self._rate_log.due(f"recheck:{reason}"):
            log.info("desk relay: chan %d recheck failed: %s", ch.chan, reason)
        return reason is not None

    def _close(self, ch: _Channel, code: UnregisteredCode | None, *, report: bool = True) -> None:
        """Close from any thread: the writer is retired (its queued frames
        become not_begun; one being written gets its outcome from the writer)
        and the socket shut down, which ends the reader, whose `_finish`
        reports. Idempotent; never waits."""
        with self._lock:
            if ch.phase is _Phase.CLOSED:
                return
            pending = ch.phase is _Phase.PENDING
            ch.phase = _Phase.CLOSED
            ch.close_code = code
            ch.report = ch.report and report
            if pending:
                ch.refusal = self._down
        ch.answered.set()
        if ch.writer is not None:
            ch.writer.retire()
        with contextlib.suppress(OSError):
            ch.sock.shutdown(socket.SHUT_RDWR)

    def _finish(self, ch: _Channel) -> None:
        """The channel's reader has ended: once its writer has decided every
        frame, report each event no byte of which reached the channel, then
        the `unregistered`, in that order (the hub ignores frames for a chan
        it no longer has registered)."""
        self._close(ch, None)
        if ch.writer is not None:
            ch.writer.retire()
            ch.writer.join(_WRITER_JOIN_SECONDS)
        with self._lock:
            if self._channels.get(ch.chan) is ch:
                del self._channels[ch.chan]
                self._verified -= 1
            events, report = list(ch.events), ch.report
        if report and not ch.uplink.retired:
            for request_id, frame in events:
                if frame.outcome is WriteOutcome.NOT_BEGUN:
                    self._undeliverable(ch.uplink, ch.chan, request_id, UndeliverableCode.CHANNEL_NOT_WRITTEN)
            code = ch.close_code or UnregisteredCode.CHANNEL_CLOSED
            if ch.writer_failed and code is UnregisteredCode.CHANNEL_CLOSED:
                # The writer shuts the socket down before it stops, so the reader
                # may see the end first; the write failure is the cause.
                code = UnregisteredCode.CHANNEL_WRITE_FAILED
            if self._rate_log.due(f"unregistered:{code.value}"):
                log.info("desk relay: chan %d unregistered: %s", ch.chan, code.value)
            self._report(ch.uplink, LinkType.UNREGISTERED, chan=ch.chan, reason=code)
        with contextlib.suppress(OSError):
            ch.sock.close()
