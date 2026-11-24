"""The relay's side of the Mac link (docs/voice-desk.md, "Admission and
proof of possession", "Deadlines, heartbeats and backpressure", "Link
freshness and sleep").

`RelayLink` is one attempt and, once the hub welcomes it, one link object:
the `Uplink` relay_channels registers channels on. It runs the `ssh`
relay_ssh prepared (no shell) as a child through child_process, says
`link_hello`, signs the hub's challenge with the desk key, and once
welcomed:

- writes every frame to ssh's stdin through one LinkWriter (64 frames and
  1 MiB, each with 5 s of awake time from its submit); a writer deadline or
  a failed write retires the link;
- pings every 15 s whatever else it sends, answers the hub's pings, and
  retires after 45 s without a frame from the hub;
- keeps its ping records and `acked_at` on CLOCK_MONOTONIC, which on macOS
  counts sleep, and dispatches a hub frame other than a `pong` only while
  `CLOCK_MONOTONIC - acked_at` is under 40 s; otherwise it retires at once,
  dispatching nothing more;
- hands channel frames to relay_channels and answers `presence_query`
  itself through peer_mac.

The whole handshake, from spawning ssh to `link_welcome`, has 30 s on
CLOCK_MONOTONIC; ssh then gets child_process's cleanup. `RelayConnector`
opens links one after another for the relay's life, with the backoff each
end calls for. Logs carry codes, ssh exit statuses, classified stderr codes
and link id prefixes, never text from ssh, a channel or the hub. Standard
library only.
"""

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import contextlib
import functools
import logging
import os
import random
import select
import threading
import time
from typing import Callable

from . import peer_mac
from .child_process import spawn as spawn_child
from .link_proof import SIGN_DEADLINE_SECONDS, SigningFailed, proof_message, sign as sign_proof
from .link_protocol import (
    BACKOFF_JITTER, BACKOFF_MAX_SECONDS, BACKOFF_RESET_AFTER_SECONDS, BACKOFF_START_SECONDS,
    FIXED_BACKOFF_SECONDS, FRAME_DEADLINE_SECONDS, HANDSHAKE_DEADLINE_SECONDS, HEARTBEAT_SECONDS,
    HUB_NOT_RUNNING_BACKOFF_MAX_SECONDS, IDLE_TIMEOUT_SECONDS, LINK_QUEUE_BYTES, LINK_QUEUE_FRAMES, LINK_VERSIONS,
    MAX_LINK_LINE_BYTES, PRESENCE_CODES, LineSplitter, LinkFrameError, LinkType, RefusedCode, Side, decode_frame,
    encode_frame,
)
from .link_writer import LinkWriter, PipeTarget
from .rate_log import RateLog
from .relay_channels import RelayRefusal
from .relay_ssh import MAX_SSH_OUTPUT_BYTES, SshInvocation, SshStderr, classify_stderr, prepare_ssh

log = logging.getLogger("llm_switchboard.desk.relay")

# The freshness rule's bound: the hub retires a link after 45 s without a
# frame, so 40 s since an acknowledged frame proves it hasn't.
FRESHNESS_SECONDS = 40
MAX_PING_RECORDS = 4
SSH_ERROR_EXIT = 255
_POLL_SECONDS = 0.25
_READ_BYTES = 65536
# After ssh's stdout ends before any link frame: how long its exit status
# and stderr get to arrive, so the end can be classified.
_EXIT_WAIT_SECONDS = 2
_MAX_DOUBLINGS = 16


def link_clock() -> float:
    """CLOCK_MONOTONIC, which on macOS keeps counting while the Mac sleeps."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


class LinkEnd(StrEnum):
    """Why an attempt ended, besides the hub's `link_refused` codes; also its log code."""
    HANDSHAKE_TIMEOUT = "handshake_timeout"
    SSH_CONFIG_UNSUPPORTED = "ssh_config_unsupported"
    SSH_UNAVAILABLE = "ssh_unavailable"
    # ssh's stdout ended before any link frame: ssh's own failure (255),
    # an admission closed without a refusal (0), or a signal.
    SSH_EXITED = "ssh_exited"
    # The forced command ran but spoke no link frame (any other exit, or a
    # first line that isn't one; a missing bin/desk-link under Python exits 2).
    FORCED_COMMAND_FAILED = "forced_command_failed"
    SIGN_FAILED = "sign_failed"
    BAD_FRAME = "bad_frame"
    LINK_CLOSED = "link_closed"
    IDLE = "idle"
    STALE = "stale"
    WRITER_FAILED = "writer_failed"
    QUEUE_FULL = "queue_full"
    RETIRED = "retired"
    RELAY_ERROR = "relay_error"


@dataclass(frozen=True)
class Attempt:
    """One attempt's end. `code` is a LinkEnd or a RefusedCode value."""
    code: str
    returncode: int | None = None
    stderr: SshStderr | None = None
    # Link-clock seconds from `link_welcome` to the end; None when never welcomed.
    up_seconds: float | None = None
    # A `version` refusal's list of the hub's versions (validated by the decoder).
    hub_versions: tuple[int, ...] | None = None


_FIXED = frozenset({RefusedCode.VERSION, RefusedCode.NOT_AUTHORIZED, RefusedCode.PROOF_FAILED,
                    RefusedCode.VERIFIER_UNAVAILABLE, LinkEnd.SSH_CONFIG_UNSUPPORTED, LinkEnd.FORCED_COMMAND_FAILED})
_FIXED_SSH_STDERR = frozenset({SshStderr.PERMISSION_DENIED, SshStderr.HOST_KEY})


def fixed_backoff(attempt: Attempt) -> bool:
    """Ends that retrying soon can't fix: the fixed 10 minutes."""
    return attempt.code in _FIXED or (attempt.code == LinkEnd.SSH_EXITED and attempt.returncode == SSH_ERROR_EXIT
                                      and attempt.stderr in _FIXED_SSH_STDERR)


class Backoff:
    """1 s doubling to 60 s with ±20 % jitter (`hub_not_running` doubling
    to 5 minutes), reset once a link has stayed up 60 s; the fixed ends
    wait 10 minutes. `rng` returns a float in [0, 1)."""

    def __init__(self, rng: Callable[[], float] = random.random):
        self._rng = rng
        self._doublings = 0

    def after(self, attempt: Attempt) -> float:
        if attempt.up_seconds is not None and attempt.up_seconds >= BACKOFF_RESET_AFTER_SECONDS:
            self._doublings = 0
        if fixed_backoff(attempt):
            return float(FIXED_BACKOFF_SECONDS)
        cap = HUB_NOT_RUNNING_BACKOFF_MAX_SECONDS if attempt.code == RefusedCode.HUB_NOT_RUNNING else BACKOFF_MAX_SECONDS
        base = min(cap, BACKOFF_START_SECONDS * 2 ** self._doublings)
        self._doublings = min(self._doublings + 1, _MAX_DOUBLINGS)
        return base * (1 + BACKOFF_JITTER * (2 * self._rng() - 1))


class _Ended(Exception):
    """Unwinds the reader to its end; args[0] is the end's code."""


class _Eof(Exception):
    pass


class RelayLink:
    """One attempt, and once welcomed one link (relay_channels' `Uplink`).
    `run()` blocks until it ends. `clock` is CLOCK_MONOTONIC for the
    handshake, the idle rule and the freshness rule; `awake_clock` is
    awake time, for the ssh writer's deadlines. `sign(message,
    deadline_seconds=)` makes the proof (default: the desk key through
    ssh-keygen); `spawn`, `target_factory` and `reader` are seams for tests."""

    def __init__(self, channels, invocation: SshInvocation, *, machine: str, relay_build: str = "",
                 versions=LINK_VERSIONS, reader=None, sign=None, spawn=spawn_child, target_factory=PipeTarget,
                 clock: Callable[[], float] = link_clock, awake_clock: Callable[[], float] = time.monotonic,
                 handshake_seconds: float = HANDSHAKE_DEADLINE_SECONDS, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 idle_seconds: float = IDLE_TIMEOUT_SECONDS, write_deadline_seconds: float = FRAME_DEADLINE_SECONDS):
        self._channels = channels
        self._invocation = invocation
        self.host = invocation.host
        self._machine = machine
        self._relay_build = relay_build
        self._versions = tuple(versions)
        self._reader = reader
        self._sign = sign or functools.partial(sign_proof, invocation.key)
        self._spawn = spawn
        self._target_factory = target_factory
        self._clock = clock
        self._awake_clock = awake_clock
        self._handshake_seconds = handshake_seconds
        self._heartbeat = heartbeat_seconds
        self._idle = idle_seconds
        self._write_deadline = write_deadline_seconds
        # Set from `link_challenge`, before any channel can see this object.
        self.epoch = ""
        self.version: int | None = None
        self.link_id: str | None = None
        self.child = None
        self._lock = threading.Lock()
        self._retired = False
        self._end: str | None = None
        self._writer: LinkWriter | None = None
        # The reader's wake pipe, made by `run()` so a link never run holds no descriptor.
        self._wake_r = self._wake_w = -1
        self._stdout_fd = -1
        self._splitter = LineSplitter(MAX_LINK_LINE_BYTES)
        self._lines: deque[bytes] = deque()
        self._seen_frame = False
        self._stderr = bytearray()
        self._stderr_done = threading.Event()
        self._hub_versions: tuple[int, ...] | None = None
        # The reader thread's own state (records also read by `ping_records`).
        self._pings_sent = 0
        self._records: dict[int, float] = {}
        self.acked_at: float | None = None
        self._fresh_for = FRESHNESS_SECONDS
        self._ping_every = heartbeat_seconds
        self._last_received = self._last_ping = 0.0
        self._handlers = {LinkType.PING: self._on_ping, LinkType.PRESENCE_QUERY: self._on_presence_query,
                          **{kind: self._to_channels for kind in channels.hub_frames}}

    # -- the Uplink surface (any thread) ---------------------------------------------

    @property
    def retired(self) -> bool:
        with self._lock:
            return self._retired

    def submit(self, kind: LinkType, **fields) -> bool:
        """Queue one frame to the hub; False when the link has retired or
        ssh's queue is full. LinkFrameError for a frame the hub would refuse."""
        data = encode_frame(kind, Side.RELAY, v=self.version, **fields)
        with self._lock:
            writer = None if self._retired else self._writer
        return writer is not None and writer.submit(data).refusal is None

    def retire(self, reason: str) -> None:
        """Retire from any thread (relay_channels, a stop); never waits."""
        if self._stop(LinkEnd.RETIRED):
            log.info("desk relay: link %s retired: %s", (self.link_id or "-")[:8], reason)

    def ping_records(self) -> dict[int, float]:
        """The unmatched ping records, `{n: t}`."""
        with self._lock:
            return dict(self._records)

    # -- the attempt -------------------------------------------------------------------

    def run(self) -> Attempt:
        """Spawn ssh, run the handshake, then the link until it ends. Never raises."""
        started = self._clock()
        welcomed_at = None
        with self._lock:
            if self._retired:  # retired before it ran: nothing was started
                return Attempt(self._end)
            self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        try:
            self._start_child()
        except Exception as exc:
            code = LinkEnd.SSH_UNAVAILABLE if isinstance(exc, OSError) else LinkEnd.RELAY_ERROR
            if code is LinkEnd.RELAY_ERROR:
                log.exception("desk relay: starting ssh raised")
            self._stop(code)
            if self.child is not None:  # spawned, then the writer or a thread failed to start
                self.child.cleanup()
            self._close_fds()
            self._channels.link_down(self, RelayRefusal.LINK_DOWN)
            return Attempt(code)
        try:
            self._handshake(started + self._handshake_seconds)
            welcomed_at = self._clock()
            log.info("desk relay: link %s up (version %d)", self.link_id[:8], self.version)
            self._channels.link_up(self)
            self._serve()
        except _Eof:
            self._stop(self._eof_code())
        except _Ended as ended:
            self._stop(ended.args[0])
        except Exception:
            log.exception("desk relay: link raised")
            self._stop(LinkEnd.RELAY_ERROR)
        ended_at = self._clock()
        code = self._end
        if code == LinkEnd.WRITER_FAILED and not self._seen_frame:
            # ssh gone before answering: its exit says why, not the broken pipe.
            code = self._eof_code(LinkEnd.WRITER_FAILED)
        refusal = RelayRefusal.LINK_VERSION if code == RefusedCode.VERSION else RelayRefusal.LINK_DOWN
        try:
            self._channels.link_down(self, refusal)
        except Exception:
            log.exception("desk relay: closing channels raised")
        returncode, stderr = self._finish_child(code)
        if welcomed_at is not None:
            log.info("desk relay: link %s down: %s", self.link_id[:8], code)
        return Attempt(code, returncode, stderr, None if welcomed_at is None else ended_at - welcomed_at,
                       self._hub_versions)

    def _start_child(self) -> None:
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        try:
            self.child = self._spawn(self._invocation.argv, env=self._invocation.env, stdin=in_r, stdout=out_w,
                                     stderr=err_w)
        except BaseException:
            for fd in (in_w, out_r, err_r):
                os.close(fd)
            raise
        finally:
            for fd in (in_r, out_w, err_w):
                os.close(fd)
        os.set_blocking(out_r, False)
        self._stdout_fd = out_r
        self._writer = LinkWriter(self._target_factory(in_w), max_frames=LINK_QUEUE_FRAMES,
                                  max_bytes=LINK_QUEUE_BYTES, deadline_seconds=self._write_deadline,
                                  clock=self._awake_clock, on_failure=lambda stop: self._stop(LinkEnd.WRITER_FAILED),
                                  name="desk-relay-ssh-writer")
        self._writer.start()
        threading.Thread(target=self._drain_stderr, args=(err_r,), name="desk-relay-ssh-stderr", daemon=True).start()

    def _drain_stderr(self, fd: int) -> None:
        """Keep the first 64 KiB of ssh's stderr, for classification only;
        drain the rest so ssh never blocks on it."""
        try:
            while chunk := os.read(fd, _READ_BYTES):
                room = MAX_SSH_OUTPUT_BYTES - len(self._stderr)
                if room > 0:
                    self._stderr.extend(chunk[:room])
        except OSError:
            pass
        finally:
            os.close(fd)
            self._stderr_done.set()

    def _eof_code(self, running: str = LinkEnd.SSH_EXITED) -> str:
        """ssh's stdout ended (or its stdin broke). Before any link frame,
        its exit status says why; `running` when it hasn't exited."""
        if self._seen_frame:
            return LinkEnd.LINK_CLOSED
        status = self.child.wait_until(time.monotonic() + _EXIT_WAIT_SECONDS)
        if status is None:
            return running
        if status <= 0 or status == SSH_ERROR_EXIT:
            return LinkEnd.SSH_EXITED
        return LinkEnd.FORCED_COMMAND_FAILED

    def _finish_child(self, code: str) -> tuple[int | None, SshStderr | None]:
        """child_process's cleanup on every path; the exit and its stderr
        code when ssh ended before any link frame."""
        stderr = None
        if code in (LinkEnd.SSH_EXITED, LinkEnd.FORCED_COMMAND_FAILED):
            self._stderr_done.wait(_EXIT_WAIT_SECONDS)
            stderr = classify_stderr(bytes(self._stderr))
        self.child.cleanup()
        self._close_fds()
        return self.child.popen.returncode, stderr

    def _close_fds(self) -> None:
        with self._lock:
            fds = [fd for fd in (self._stdout_fd, self._wake_r, self._wake_w) if fd >= 0]
            self._stdout_fd = self._wake_r = self._wake_w = -1
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _stop(self, code: str) -> bool:
        """The first end wins; retires the writer (its queued frames become
        not_begun, a frame being written gets its outcome from the writer,
        which then closes ssh's stdin) and wakes the reader."""
        with self._lock:
            if self._retired:
                return False
            self._retired = True
            self._end = code
            writer = self._writer
            if self._wake_w >= 0:
                with contextlib.suppress(OSError):
                    os.write(self._wake_w, b"x")
        if writer is not None:
            writer.retire()
        return True

    # -- reading -----------------------------------------------------------------------

    def _next_line(self, deadline: float | None) -> bytes | None:
        """The next whole line from ssh. With a deadline (the handshake) it
        waits for one or raises HANDSHAKE_TIMEOUT; without, it returns None
        after one poll slice so the caller can tick."""
        while not self._lines:
            if self.retired:
                raise _Ended(self._end)
            now = self._clock()
            if deadline is not None and now >= deadline:
                raise _Ended(LinkEnd.HANDSHAKE_TIMEOUT)
            timeout = _POLL_SECONDS if deadline is None else min(_POLL_SECONDS, deadline - now)
            if not self._readable(timeout):
                if deadline is None:
                    return None
                continue
            try:
                data = os.read(self._stdout_fd, _READ_BYTES)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if not data:
                raise _Eof()  # a partial line left in the splitter is dropped
            try:
                self._lines.extend(self._splitter.feed(data))
            except LinkFrameError:
                raise _Ended(LinkEnd.BAD_FRAME) from None
        return self._lines.popleft()

    def _readable(self, timeout: float) -> bool:
        poller = select.poll()
        poller.register(self._stdout_fd, select.POLLIN)
        poller.register(self._wake_r, select.POLLIN)
        ready = {fd for fd, _ in poller.poll(max(0, int(timeout * 1000)))}
        if self._wake_r in ready:
            with contextlib.suppress(OSError):
                while os.read(self._wake_r, 64):
                    pass
        return self._stdout_fd in ready

    def _handshake_frame(self, deadline: float, v: int | None) -> tuple[LinkType, dict]:
        line = self._next_line(deadline)
        try:
            kind, fields = decode_frame(line, Side.HUB, v=v)
        except LinkFrameError:
            raise _Ended(LinkEnd.BAD_FRAME if self._seen_frame else LinkEnd.FORCED_COMMAND_FAILED) from None
        self._seen_frame = True
        if kind is LinkType.LINK_REFUSED:
            if fields["reason"] == RefusedCode.VERSION:
                self._hub_versions = tuple(fields["versions"])
            raise _Ended(RefusedCode(fields["reason"]))
        return kind, fields

    # -- the handshake -------------------------------------------------------------------

    def _handshake(self, deadline: float) -> None:
        self._send(LinkType.LINK_HELLO, versions=list(self._versions), relay_build=self._relay_build,
                   host=self.host, machine=self._machine)
        kind, challenge = self._handshake_frame(deadline, v=None)
        if kind is not LinkType.LINK_CHALLENGE or challenge["version"] not in self._versions:
            raise _Ended(LinkEnd.BAD_FRAME)
        self.version, self.epoch = challenge["version"], challenge["epoch"]
        message = proof_message(version=self.version, host=self.host, machine=self._machine, epoch=self.epoch,
                                nonce=challenge["nonce"])
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _Ended(LinkEnd.HANDSHAKE_TIMEOUT)
        try:
            signature = self._sign(message, deadline_seconds=min(SIGN_DEADLINE_SECONDS, remaining))
        except SigningFailed as exc:
            log.warning("desk relay: proof not signed: %s", exc.args[0])
            raise _Ended(LinkEnd.SIGN_FAILED) from None
        # Read before the proof is enqueued: no byte of it reaches the hub
        # earlier, so the hub's welcome acknowledges a receipt after this.
        proof_at = self._clock()
        self._send(LinkType.LINK_PROOF, signature=signature)
        kind, welcome = self._handshake_frame(deadline, v=self.version)
        if kind is not LinkType.LINK_WELCOME:
            raise _Ended(LinkEnd.BAD_FRAME)
        self.link_id = welcome["link_id"]
        self.acked_at = proof_at
        # A hub announcing a shorter idle rule shortens the bound with it, so
        # the rule's proof still holds; a longer one changes nothing.
        self._fresh_for = min(FRESHNESS_SECONDS,
                              welcome["idle_timeout_seconds"] - (IDLE_TIMEOUT_SECONDS - FRESHNESS_SECONDS))
        self._ping_every = min(self._heartbeat, welcome["heartbeat_seconds"])

    def _send(self, kind: LinkType, **fields) -> None:
        if not self.submit(kind, **fields):
            raise _Ended(self._end or LinkEnd.QUEUE_FULL)

    # -- the established link ----------------------------------------------------------------

    def _serve(self) -> None:
        self._last_received = self._last_ping = self._clock()
        while True:
            self._tick()
            line = self._next_line(None)
            if line is None:
                continue
            self._last_received = self._clock()
            try:
                kind, fields = decode_frame(line, Side.HUB, v=self.version)
            except LinkFrameError:
                raise _Ended(LinkEnd.BAD_FRAME) from None
            if kind is LinkType.PONG:
                self._on_pong(fields["n"])
                continue
            if self._clock() - self.acked_at >= self._fresh_for:
                raise _Ended(LinkEnd.STALE)
            if self.retired:
                raise _Ended(self._end)
            handler = self._handlers.get(kind)
            if handler is None:
                raise _Ended(LinkEnd.BAD_FRAME)  # a handshake frame on an established link
            handler(kind, fields)

    def _tick(self) -> None:
        """The idle rule, the expiry of old ping records, and the 15 s ping."""
        now = self._clock()
        if now - self._last_received >= self._idle:
            raise _Ended(LinkEnd.IDLE)
        with self._lock:
            for n in [n for n, t in self._records.items() if now - t >= self._fresh_for]:
                del self._records[n]
        if now - self._last_ping >= self._ping_every:
            self._ping()

    def _ping(self) -> None:
        n = self._pings_sent + 1
        # Recorded before the ping is enqueued, so `t` is no later than the
        # hub's receipt however long anything is delayed after this.
        t = self._clock()
        with self._lock:
            self._records[n] = t
            while len(self._records) > MAX_PING_RECORDS:
                del self._records[min(self._records)]
        self._last_ping = t
        if self.submit(LinkType.PING, n=n):
            self._pings_sent = n
        else:
            # Skipped (a full queue): no record; the idle rule ends a link that stays stuck.
            with self._lock:
                self._records.pop(n, None)

    def _on_pong(self, n: int) -> None:
        if n > self._pings_sent:
            raise _Ended(LinkEnd.BAD_FRAME)
        with self._lock:
            t = self._records.pop(n, None)
        # Already matched, dropped or expired: ignored, changes nothing.
        if t is not None and self._clock() - t < self._fresh_for:
            self.acked_at = max(self.acked_at, t)

    def _on_ping(self, kind: LinkType, fields: dict) -> None:
        self.submit(LinkType.PONG, n=fields["n"])  # skipped when the queue is full

    def _on_presence_query(self, kind: LinkType, fields: dict) -> None:
        process = peer_mac.process_presence(fields["channel_pid"], fields["channel_start"], reader=self._reader)
        if not self.submit(LinkType.PRESENCE, query_id=fields["query_id"], state=PRESENCE_CODES[process].value):
            raise _Ended(LinkEnd.QUEUE_FULL)  # losing it would lose evidence

    def _to_channels(self, kind: LinkType, fields: dict) -> None:
        try:
            self._channels.handle_hub_frame(self, kind, fields)
        except LinkFrameError:
            raise _Ended(LinkEnd.BAD_FRAME) from None


class RelayConnector:
    """Links one after another for the relay's life: a fresh route from
    `prepare(config)` (relay_ssh) for each attempt, then the wait its end
    calls for. Fixed ends are logged once per change, others at most once
    a minute per code."""

    def __init__(self, config, channels, *, machine: str, relay_build: str = "", prepare=prepare_ssh,
                 link_factory=RelayLink, backoff: Backoff | None = None, **link_options):
        self._config = config
        self._channels = channels
        self._machine = machine
        self._relay_build = relay_build
        self._prepare = prepare
        self._link_factory = link_factory
        self._backoff = backoff or Backoff()
        self._link_options = link_options
        self._rate_log = RateLog()
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._current: RelayLink | None = None
        self._last_fixed: tuple | None = None

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                attempt = self._attempt()
            except Exception:
                log.exception("desk relay: link attempt raised")
                attempt = Attempt(LinkEnd.RELAY_ERROR)
            delay = self._backoff.after(attempt)
            self._log(attempt, delay)
            self._stopping.wait(delay)

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            link = self._current
        if link is not None:
            link.retire("relay stopping")

    def _attempt(self) -> Attempt:
        try:
            invocation = self._prepare(self._config)
        except (ValueError, KeyError, OSError):
            return Attempt(LinkEnd.SSH_CONFIG_UNSUPPORTED)
        link = self._link_factory(self._channels, invocation, machine=self._machine, relay_build=self._relay_build,
                                  **self._link_options)
        with self._lock:
            # A link never run holds nothing, so one made as the relay stops is just dropped.
            if self._stopping.is_set():
                return Attempt(LinkEnd.RETIRED)
            self._current = link
        try:
            return link.run()
        finally:
            with self._lock:
                self._current = None

    def _log(self, attempt: Attempt, delay: float) -> None:
        detail = ""
        if attempt.returncode is not None and attempt.stderr is not None:
            detail = f" (ssh exit {attempt.returncode}, {attempt.stderr.value})"
        elif attempt.hub_versions is not None:
            detail = f" (hub versions {list(attempt.hub_versions)})"
        if fixed_backoff(attempt):
            key = (attempt.code, attempt.stderr)
            if key != self._last_fixed:
                log.warning("desk relay: link refused: %s%s; next try in %d minutes", attempt.code, detail,
                            FIXED_BACKOFF_SECONDS // 60)
            self._last_fixed = key
            return
        self._last_fixed = None
        if self._rate_log.due(f"end:{attempt.code}:{attempt.stderr}"):
            log.info("desk relay: link attempt ended: %s%s; next try in %.0f s", attempt.code, detail, delay)
