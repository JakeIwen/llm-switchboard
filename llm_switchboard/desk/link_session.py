"""One admitted Mac link on the hub, and the proxies its registrations use
(docs/voice-desk.md, "Link objects, replacement and the fence on old
work" and "Hub side").

A `LinkSession` is one published link: its host, the key it verified and
that key's fingerprint, the machine, its socket and one `LinkWriter`, the
highest `chan` a `register` named on it, its presence query counter and
its `retired` flag (set only by `link_hub.LinkHub`, under the links
lock). Nothing looks a host's link up by name to act on it: a proxy, a
presence query and a queued frame each hold the object they began on, so
writing to a retired object fails at once (`not_begun`) without touching
its successor.

A `RemoteChannel` is a Mac registration's entry in `Hub._connections`,
with the `ChannelConnection` surface the hub already calls plus
`send_event`, so `Hub.send`, `_drop_connection`, `_drop_unenrolled` and
`_send_heartbeats` work for Mac targets without branches. Only
`send_event` (and a presence query) waits on the link. Standard library
only.
"""

from collections import deque
import threading
from typing import Callable

from .link_proof import Signer
from .link_protocol import (
    FRAME_DEADLINE_SECONDS, LINK_QUEUE_BYTES, LINK_QUEUE_FRAMES, MAX_COUNTER, MAX_LINK_REGISTRATIONS,
    MAX_PRESENCE_QUERIES, MAX_REGISTERS_PER_MINUTE, REPLY_REFUSAL_CODES, SEND_GUARD_SECONDS, DropCode, LinkFrameError,
    LinkType, PresenceState, Side, encode_frame,
)
from .link_writer import Frame, LinkWriter, SocketTarget, SubmitRefusal
from .model import ReplyRefusal, WriteOutcome, is_request_id
from .protocol import MessageType

_MINUTE = 60.0


class _PresenceWait:
    def __init__(self):
        self.answered = threading.Event()
        self.state: PresenceState | None = None


class LinkSession:
    def __init__(self, *, link_id: str, signer: Signer, machine: str, version: int, sock,
                 clock: Callable[[], float], retire: Callable[["LinkSession", str], None]):
        self.link_id = link_id
        self.host = signer.host
        self.blob = signer.blob
        self.fingerprint = signer.fingerprint
        self.machine = machine
        self.version = version
        self.sock = sock
        # Set only under LinkHub's links lock (LinkHub.retire).
        self.retired = False
        self._clock = clock
        self._retire = retire
        self._lock = threading.Lock()
        self.highest_chan = 0
        self._channels: dict[int, "RemoteChannel"] = {}
        self._queries_issued = 0
        self._queries: dict[int, _PresenceWait] = {}
        self._register_times: deque[float] = deque()
        self._pings_sent = 0
        self._finalized = False
        self.last_received = clock()
        self.last_sent = clock()
        self.writer = LinkWriter(SocketTarget(sock), max_frames=LINK_QUEUE_FRAMES, max_bytes=LINK_QUEUE_BYTES,
                                 deadline_seconds=FRAME_DEADLINE_SECONDS, clock=clock,
                                 on_failure=lambda stop: retire(self, f"writer {stop.value}"),
                                 name="desk-link-writer")

    # -- frames to the relay ---------------------------------------------------

    def submit(self, kind: LinkType, **fields) -> Frame | None:
        """Queue one frame; None when it can't be encoded. A full queue
        retires the link (the frame comes back `not_begun`)."""
        try:
            data = encode_frame(kind, Side.HUB, v=self.version, **fields)
        except LinkFrameError:
            return None
        frame = self.writer.submit(data)
        if frame.refusal is SubmitRefusal.FULL:
            self._retire(self, "writer queue full")
        elif frame.refusal is None:
            self.last_sent = self._clock()
        return frame

    def ping(self) -> None:
        with self._lock:
            self._pings_sent += 1
            n = self._pings_sent
        self.submit(LinkType.PING, n=n)

    def pong_known(self, n: int) -> bool:
        """A pong for a ping this link never sent is a protocol violation."""
        with self._lock:
            return n <= self._pings_sent

    # -- registrations -----------------------------------------------------------

    def claim_chan(self, chan: int) -> bool:
        """A `register` must name a `chan` above every one this link has
        seen (the relay's counter never recurs)."""
        with self._lock:
            if chan <= self.highest_chan:
                return False
            self.highest_chan = chan
            return True

    def chan_seen(self, chan: int) -> bool:
        with self._lock:
            return chan <= self.highest_chan

    def register_allowed(self) -> bool:
        """120 `register` frames a minute per link."""
        now = self._clock()
        with self._lock:
            while self._register_times and now - self._register_times[0] >= _MINUTE:
                self._register_times.popleft()
            if len(self._register_times) >= MAX_REGISTERS_PER_MINUTE:
                return False
            self._register_times.append(now)
            return True

    def has_room(self) -> bool:
        with self._lock:
            return len(self._channels) < MAX_LINK_REGISTRATIONS

    def add_channel(self, channel: "RemoteChannel") -> bool:
        """False once the link's registrations were taken by its
        finalizer: the caller drops the registration it just made."""
        with self._lock:
            if self._finalized:
                return False
            self._channels[channel.chan] = channel
            return True

    def channel(self, chan: int) -> "RemoteChannel | None":
        with self._lock:
            return self._channels.get(chan)

    def forget_channel(self, chan: int) -> None:
        with self._lock:
            self._channels.pop(chan, None)

    def finalize(self) -> list["RemoteChannel"]:
        """Once, after retirement: the registrations to drop, and every
        pending presence query answered `unverifiable`."""
        with self._lock:
            if self._finalized:
                return []
            self._finalized = True
            channels = list(self._channels.values())
            self._channels.clear()
            waits = list(self._queries.values())
            self._queries.clear()
        for wait in waits:
            wait.answered.set()
        return channels

    # -- presence --------------------------------------------------------------

    def query_presence(self, channel_pid: int, channel_start: str, timeout: float) -> PresenceState:
        """Is that Mac channel process still running? Bound to this link and
        its `query_id`; `unverifiable` beyond 4 outstanding, on a retired
        link, or when no answer comes within `timeout` of enqueueing."""
        with self._lock:
            if self._finalized or len(self._queries) >= MAX_PRESENCE_QUERIES or self._queries_issued >= MAX_COUNTER:
                return PresenceState.UNVERIFIABLE
            self._queries_issued += 1
            query_id = self._queries_issued
            wait = self._queries[query_id] = _PresenceWait()
        frame = self.submit(LinkType.PRESENCE_QUERY, query_id=query_id, channel_pid=channel_pid,
                            channel_start=channel_start)
        if frame is not None and frame.refusal is None:
            wait.answered.wait(timeout)
        with self._lock:
            self._queries.pop(query_id, None)
        return wait.state or PresenceState.UNVERIFIABLE

    def answer_presence(self, query_id: int, state: PresenceState) -> bool:
        """False for a `query_id` above the highest issued (the link
        closes); an answer to one no longer outstanding is ignored."""
        with self._lock:
            if query_id > self._queries_issued:
                return False
            wait = self._queries.get(query_id)
            if wait is not None and wait.state is None:
                wait.state = state
                wait.answered.set()
            return True


# What the hub's free-text drop reasons become on the wire.
_DROP_CODES = {"unenrolled": DropCode.UNENROLLED}


class RemoteChannel:
    """A Mac registration's connection. Holds the link object it began on."""

    def __init__(self, conn_id: int, link: LinkSession, chan: int, session_id: str, label: str, cwd: str):
        self.conn_id = conn_id
        self.link = link
        self.chan = chan
        self.session_id = session_id
        # The session file's name and cwd as the relay read them (the
        # session's label and folder on the desk).
        self.label = label
        self.cwd = cwd
        # The relay ended it (`unregistered`): no `drop` is owed.
        self.relay_closed = False
        self._closed = False
        # The link has its own heartbeat; `_send_heartbeats` never sees this stale.
        self.last_sent = float("inf")

    def send(self, kind: MessageType, **fields) -> bool:
        """REPLY_RESULT is enqueued (its refusal as a wire code) and never
        waited on; PING is a no-op, since the relay pings the channel."""
        if kind is MessageType.PING:
            return True
        if kind is not MessageType.REPLY_RESULT or self.link.retired:
            return False
        request_id, reason = fields.get("request_id"), fields.get("reason")
        if not is_request_id(request_id):
            return False
        code = None if reason is None else REPLY_REFUSAL_CODES[ReplyRefusal(reason)].value
        frame = self.link.submit(LinkType.REPLY_RESULT, chan=self.chan, request_id=request_id,
                                 ok=bool(fields.get("ok")), reason=code)
        return frame is not None and frame.refusal is None

    def send_event(self, request_id: str, content: str) -> WriteOutcome:
        """The link frame's outcome, from its writer (guarded at 6 s)."""
        if self.link.retired or self._closed:
            return WriteOutcome.NOT_BEGUN
        frame = self.link.submit(LinkType.EVENT, chan=self.chan, request_id=request_id, content=content)
        if frame is None:
            return WriteOutcome.NOT_BEGUN
        return frame.wait(SEND_GUARD_SECONDS)

    def close(self, reason: str | None = None) -> None:
        """Enqueue `drop` when the link is live and the relay still holds
        the channel; never waits."""
        if self._closed:
            return
        self._closed = True
        self.link.forget_channel(self.chan)
        if not self.relay_closed and not self.link.retired:
            code = _DROP_CODES.get(reason or "", DropCode.HUB_RECHECK_FAILED)
            self.link.submit(LinkType.DROP, chan=self.chan, reason=code.value)
