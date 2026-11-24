"""The Mac relay's surroundings for tests: an isolated Mac home whose kernel
facts come from a fake libproc reader (so the tests run on Linux too), and
`FakeUplink`, the hub's end of a link as `relay_channels` sees it, which
records every frame the relay sends after checking it the way the hub's
decoder would.

For the relay's link (relay_link): `pump_invocation` runs the real pump
(`bin/desk-link`) in place of ssh and `script_invocation` a Python script;
`FakeHub` is the hub's end of link.sock behind that pump, `FakeChannels`
records what the link hands relay_channels, and `fake_sign` stands in for
ssh-keygen where the proof isn't what is tested.

`MacHome` builds the same launch `tests/test_desk_peer_mac.py` checks (the
framework interpreter, an installed claude, a session file matching the
parent's start); it is written again here rather than imported from that
test module, which has no shared fixture.

`JoinedCase` joins the whole link for end-to-end tests: a real hub (Pi
home), the real pump in place of ssh, the real relay (`relay.serve` with
`RelayChannels` and `RelayConnector`, the kernel faked behind peer_mac's
reader), and the real `channel.HubLink` (`DeskChannel`) on the relay's
socket, writing what Claude would read into `ClaudeStdin`.
"""

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest

from desk_fixtures import SESSION_A, SESSION_C, Clock, HubCase, desk_config, make_home, request_id, wait_until
from link_peer import MAC, MACHINE, LinkHubCase, fake_signature
from llm_switchboard.desk.channel import HubLink, StdoutWriter
from llm_switchboard.desk.link_protocol import (
    LINK_VERSIONS, MAX_LINK_LINE_BYTES, VERSION_FREE, LineSplitter, LinkFrameError, LinkType, Side, decode_frame,
    encode_frame,
)
from llm_switchboard.desk.link_writer import PipeTarget, SocketTarget
from llm_switchboard.desk.mac_process import MacProcess, ProcessStamp
from llm_switchboard.desk.relay import serve as serve_relay
from llm_switchboard.desk.relay_channels import RelayChannels, RelayRefusal
from llm_switchboard.desk.relay_link import Attempt, Backoff, RelayConnector, RelayLink
from llm_switchboard.desk.relay_ssh import SshInvocation

EPOCH = "e" * 32
CLAUDE_PID, CHANNEL_PID = 42, 43


class FakeKernel:
    """`LibprocReader`'s surface over fixed records."""

    def __init__(self, records):
        self.records = {p.stamp.pid: p for p in records}
        self.cred = (records[0].stamp.pid, os.getuid())
        self.failure = None

    def credentials(self, sock):
        return self.cred

    def process(self, pid):
        if self.failure:
            raise self.failure
        if pid not in self.records:
            raise ProcessLookupError(pid)
        return self.records[pid]

    def stamp(self, pid):
        return self.process(pid).stamp


class MacHome:
    """A Mac home with one flagged claude (pid 42) and its desk channel
    (pid 43) for `session_id`. The channel guard of a real `HubLink` in this
    process reads the file for its own parent, which `publish_guard` writes."""

    def __init__(self, case, session_id: str = SESSION_A):
        home = make_home(case)
        self.home = home
        self.session_id = session_id
        self.config = replace(desk_config(home), repo=home / "checkout")
        c = self.config
        interpreter = home / "Python.framework/Versions/3.13/bin/python3.13"
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
        pinned = Path(c.channel_argv[0])
        pinned.parent.mkdir(parents=True)
        pinned.symlink_to(interpreter)
        app = str(interpreter.resolve().parent.parent / "Resources/Python.app/Contents/MacOS/Python")
        c.claude_versions_dir.mkdir(parents=True)
        installed = c.claude_versions_dir.resolve() / "2.1.330"
        installed.touch()
        self.sec = int(datetime(2026, 11, 23, 2, 0, 40, tzinfo=timezone.utc).timestamp())
        self.parent = MacProcess(ProcessStamp(CLAUDE_PID, 10, os.getuid(), self.sec, 444), 123, 12,
                                 str(installed), "/Users/me/bin/claude", ("claude", *c.launch_flags))
        self.child = MacProcess(ProcessStamp(CHANNEL_PID, CLAUDE_PID, os.getuid(), self.sec + 1, 0), 456, 123,
                                app, app, (app, *c.channel_argv[1:]))
        self.kernel = FakeKernel([self.child, self.parent])
        c.claude_sessions_dir.mkdir(parents=True)
        self.entry = {"sessionId": session_id, "pid": CLAUDE_PID, "procStart": "Mon Nov 23 02:00:40 2026",
                      "name": "desk work", "cwd": "/Users/me/dev/project"}
        self.publish()
        self.publish_guard()

    def publish(self, **changes) -> None:
        """The flagged claude's session file, as the relay's check reads it."""
        path = self.config.claude_sessions_dir / f"{CLAUDE_PID}.json"
        path.write_text(json.dumps(self.entry | changes))
        ns = self.parent.stamp.micros * 1000
        os.utime(path, ns=(ns, ns))

    def publish_guard(self, session_id: str | None = None) -> None:
        """The file a real channel's final guard reads (its parent is this process's parent)."""
        parent = os.getppid()
        path = self.config.claude_sessions_dir / f"{parent}.json"
        path.write_text(json.dumps({"sessionId": session_id or self.session_id, "pid": parent}))

    def remove_guard(self) -> None:
        (self.config.claude_sessions_dir / f"{os.getppid()}.json").unlink()


class FakeUplink:
    """The hub's end of one link. `answer` decides what the hub says to a
    `register`: "registered" (at once, on the submitting thread), a
    `register_refused` code, or None (no answer). `full` refuses every
    submit, as a full ssh queue would."""

    def __init__(self, relay=None, *, answer: str | None = "registered"):
        self.relay = relay
        self.answer = answer
        self.epoch = EPOCH
        self.full = False
        self.retired_reasons: list[str] = []
        self.frames: list[tuple[LinkType, dict]] = []
        self._generation = 0
        self._changed = threading.Condition()

    @property
    def retired(self) -> bool:
        return bool(self.retired_reasons)

    def retire(self, reason: str) -> None:
        with self._changed:
            self.retired_reasons.append(reason)
            self._changed.notify_all()

    def submit(self, kind: LinkType, **fields) -> bool:
        line = encode_frame(kind, Side.RELAY, v=LINK_VERSIONS[0], **fields)
        decoded = decode_frame(line, Side.RELAY, v=LINK_VERSIONS[0])  # what the hub would accept
        if self.full or self.retired:
            return False
        with self._changed:
            self.frames.append(decoded)
            self._changed.notify_all()
        if kind is LinkType.REGISTER and self.answer is not None:
            self._answer_register(fields["chan"])
        return True

    def _answer_register(self, chan: int) -> None:
        if self.answer == "registered":
            self._generation += 1
            self.send(LinkType.REGISTERED, chan=chan, generation=self._generation)
        else:
            self.send(LinkType.REGISTER_REFUSED, chan=chan, reason=self.answer)

    def send(self, kind: LinkType, **fields) -> None:
        """A hub frame to the relay, checked as the relay's decoder would."""
        line = encode_frame(kind, Side.HUB, v=LINK_VERSIONS[0], **fields)
        decoded_kind, decoded = decode_frame(line, Side.HUB, v=LINK_VERSIONS[0])
        self.relay.handle_hub_frame(self, decoded_kind, decoded)

    def of(self, kind: LinkType) -> list[dict]:
        with self._changed:
            return [fields for k, fields in self.frames if k is kind]

    def wait_for(self, kind: LinkType, predicate=lambda fields: True, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for k, fields in self.frames:
                    if k is kind and predicate(fields):
                        return fields
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"no {kind.value} frame in time: {self.frames}")
                self._changed.wait(remaining)


REPO = Path(__file__).resolve().parent.parent
LINK_ID = "ab" * 16
NONCE = "0" * 64
FAKE_SIGNATURE = "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n"
GENEROUS = 15.0


def fake_sign(message: bytes, deadline_seconds: float) -> str:
    """A well-formed armor in place of ssh-keygen, for tests about anything but the proof."""
    return FAKE_SIGNATURE


def pump_invocation(home: Path, host: str = "mac", key: Path | None = None) -> SshInvocation:
    """The real pump (`bin/desk-link`) standing in for ssh, so the relay's
    stdin and stdout reach whatever listens on `home`'s link.sock: a
    `FakeHub`, or a real hub."""
    argv = (sys.executable, "-I", "-B", str(REPO / "bin" / "desk-link"))
    return SshInvocation(argv, home, key or home / "no-key", host)


def script_invocation(home: Path, script: str, *args: str) -> SshInvocation:
    """A Python script standing in for ssh (a silent one, a failing one...)."""
    return SshInvocation((sys.executable, "-I", "-c", script, *args), home, home / "no-key", "mac")


class FakeHub:
    """The hub's end of one link on `path` (link.sock), reached by the
    relay through the real pump. It writes whatever a test says (checked
    by the relay's decoder first unless sent raw) and decodes what the
    relay sends as the hub would."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.listener.settimeout(GENEROUS)
        self.sock: socket.socket | None = None
        self.version: int | None = None
        self._splitter = LineSplitter()
        self._lines: list[bytes] = []

    def accept(self) -> None:
        if self.sock is not None:
            self.sock.close()
        self.sock, _ = self.listener.accept()
        self.sock.settimeout(GENEROUS)
        self._splitter = LineSplitter()
        self._lines = []

    def send(self, kind: LinkType, **fields) -> None:
        version = None if kind in VERSION_FREE else self.version
        self.send_raw(encode_frame(kind, Side.HUB, v=version, **fields))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read(self) -> tuple[LinkType | None, dict]:
        """The relay's next frame, (None, {}) at EOF."""
        while not self._lines:
            try:
                data = self.sock.recv(65536)
            except OSError:
                data = b""
            if not data:
                return None, {}
            self._lines.extend(self._splitter.feed(data))
        return decode_frame(self._lines.pop(0), Side.RELAY, v=self.version)

    def expect(self, kind: LinkType) -> dict:
        got, fields = self.read()
        if got is not kind:
            raise AssertionError(f"expected {kind.value}, got {got and got.value}: {fields}")
        return fields

    def challenge(self, version: int = LINK_VERSIONS[0]) -> dict:
        """Accept, read the hello, send the challenge, read the proof; the hello."""
        self.accept()
        hello = self.expect(LinkType.LINK_HELLO)
        self.send(LinkType.LINK_CHALLENGE, version=version, epoch=EPOCH, nonce=NONCE)
        self.version = version
        self.expect(LinkType.LINK_PROOF)
        return hello

    def welcome(self, *, heartbeat: int = 15, idle: int = 45) -> None:
        self.send(LinkType.LINK_WELCOME, link_id=LINK_ID, heartbeat_seconds=heartbeat, idle_timeout_seconds=idle,
                  max_line_bytes=MAX_LINK_LINE_BYTES)

    def handshake(self, **welcome) -> dict:
        hello = self.challenge()
        self.welcome(**welcome)
        return hello

    def closed(self) -> bool:
        """True once the relay's side is gone (frames still queued are skipped)."""
        while True:
            kind, _ = self.read()
            if kind is None:
                return True

    def close(self) -> None:
        for sock in (self.sock, self.listener):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()


class FakeChannels:
    """relay_channels' surface as `RelayLink` uses it, recording each call."""

    hub_frames = frozenset({LinkType.REGISTERED, LinkType.REGISTER_REFUSED, LinkType.DROP, LinkType.EVENT,
                            LinkType.REPLY_RESULT})

    def __init__(self):
        self.calls: list[tuple] = []
        self.violation = False
        self._changed = threading.Condition()

    def _record(self, *call) -> None:
        with self._changed:
            self.calls.append(call)
            self._changed.notify_all()

    def link_up(self, uplink) -> None:
        self._record("up", uplink)

    def link_down(self, uplink=None, refusal=None) -> None:
        self._record("down", uplink, refusal)

    def handle_hub_frame(self, uplink, kind, fields) -> None:
        if self.violation:
            raise LinkFrameError("test violation")
        self._record("frame", kind, fields)

    def frames(self) -> list[tuple[LinkType, dict]]:
        with self._changed:
            return [(call[1], call[2]) for call in self.calls if call[0] == "frame"]

    def wait_for(self, what: str, count: int = 1, timeout: float = GENEROUS) -> list[tuple]:
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                found = [call for call in self.calls if call[0] == what]
                if len(found) >= count:
                    return found
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"no {what} in time: {self.calls}")
                self._changed.wait(remaining)


class Gate:
    """Mixed into a real writer target. `hold(after, keep)` lets `after`
    more sends through, then makes the next one hand the kernel `keep`
    bytes (None: all) and wait for `release()` before returning, as a relay
    suspended between the kernel's accept and its own bookkeeping would.
    `full` reports the pipe or socket full."""

    def __init__(self, target):
        super().__init__(target)
        self.full = False
        self.held = threading.Event()
        self._release = threading.Event()
        self._after: int | None = None
        self._keep: int | None = None

    def hold(self, after: int = 0, keep: int | None = None) -> None:
        self._after, self._keep = after, keep
        self.held.clear()
        self._release.clear()

    def release(self) -> None:
        self._release.set()

    def send(self, view):
        if self.full:
            raise BlockingIOError
        if self._after is None:
            return super().send(view)
        if self._after > 0:
            self._after -= 1
            return super().send(view)
        self._after = None
        sent = super().send(view if self._keep is None else view[:self._keep])
        self.held.set()
        self._release.wait(GENEROUS)
        return sent

    def wait_writable(self, timeout, wake_fd):
        if self.full:
            time.sleep(min(timeout, 0.01))
        else:
            super().wait_writable(timeout, wake_fd)


class GatedPipe(Gate, PipeTarget):
    """ssh's stdin pipe, gated."""


class GatedSocket(Gate, SocketTarget):
    """A channel's socket, gated."""


class Run:
    """One `RelayLink.run()` on its own thread."""

    def __init__(self, link: RelayLink):
        self.link = link
        self.attempt: Attempt | None = None
        self.reaped_at_return = None
        self.returned_at = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        attempt = self.link.run()
        self.returned_at = time.monotonic()
        self.reaped_at_return = self.link.child is not None and self.link.child.reaped.is_set()
        self.attempt = attempt

    def ended(self) -> Attempt:
        self.thread.join(GENEROUS)
        if self.thread.is_alive():
            raise AssertionError("the attempt never ended")
        return self.attempt

    def stop(self):
        self.link.retire("test over")
        self.thread.join(GENEROUS)
        if self.link.child is not None:
            self.link.child.reaped.wait(GENEROUS)


class LinkCase(unittest.TestCase):
    def setUp(self):
        self.mac = MacHome(self)
        self.hub = FakeHub(self.mac.config.link_socket)
        self.addCleanup(self.hub.close)
        self.clock = Clock()
        self.channels = FakeChannels()
        self.targets: list[GatedPipe] = []

    def gated(self, fd) -> GatedPipe:
        target = GatedPipe(fd)
        self.targets.append(target)
        return target

    def start(self, invocation=None, *, channels=None, **options) -> Run:
        options.setdefault("sign", fake_sign)
        options.setdefault("clock", self.clock)
        options.setdefault("reader", self.mac.kernel)
        options.setdefault("target_factory", self.gated)
        link = RelayLink(channels or self.channels, invocation or pump_invocation(self.mac.home), machine=MACHINE,
                         **options)
        run = Run(link)
        self.addCleanup(run.stop)
        return run

    def up(self, *, welcome=None, **options) -> Run:
        run = self.start(**options)
        self.hub.handshake(**(welcome or {}))
        self.channels.wait_for("up")
        return run

    def read_until(self, kind: LinkType, n: int | None = None) -> tuple[dict, list[tuple]]:
        """The relay's next `kind` frame (with that `n`), and the frames before it."""
        before = []
        while True:
            got, fields = self.hub.read()
            if got is None:
                raise AssertionError(f"EOF before {kind.value}: {before}")
            if got is kind and (n is None or fields["n"] == n):
                return fields, before
            before.append((got, fields))

    def relay_ping(self) -> int:
        return self.read_until(LinkType.PING)[0]["n"]

    def round_trip(self, n: int) -> list[tuple]:
        """A hub ping answered: the relay has handled everything sent before it."""
        self.hub.send(LinkType.PING, n=n)
        return self.read_until(LinkType.PONG, n)[1]

    def event(self, n: int) -> None:
        self.hub.send(LinkType.EVENT, chan=1, request_id=request_id(n), content="hello")

    def assert_down(self, run: Run, code, refusal=RelayRefusal.LINK_DOWN) -> Attempt:
        attempt = run.ended()
        self.assertEqual(attempt.code, code)
        self.assertEqual(self.channels.calls[-1], ("down", run.link, refusal))
        return attempt

MAC_SESSION = SESSION_C

# Runs the real pump with its stderr (its one exit line) appended to the file
# argv[1], so a test can read what the pump would have told ssh.
_PUMP_STDERR_TO_FILE = (
    "import os, sys\n"
    "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
    "os.dup2(fd, 2)\n"
    "os.execv(sys.argv[2], sys.argv[2:])\n"
)


class ClaudeStdin:
    """What Claude would read from its desk channel: every line a real
    `HubLink` writes, kept in order for the test."""

    def __init__(self):
        self.lines: list[bytes] = []
        self._changed = threading.Condition()

    def write(self, data: bytes) -> int:
        with self._changed:
            self.lines.append(bytes(data))
            self._changed.notify_all()
        return len(data)

    def flush(self) -> None:
        pass

    def events(self) -> list[dict]:
        """The channel notifications' params ({content, meta: {request_id}})."""
        with self._changed:
            return [json.loads(line)["params"] for line in self.lines]

    def wait_event(self, count: int = 1, timeout: float = GENEROUS) -> dict:
        deadline = time.monotonic() + timeout
        with self._changed:
            while len(self.lines) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Claude got {len(self.lines)} of {count} events in time")
                self._changed.wait(remaining)
            return json.loads(self.lines[count - 1])["params"]


class DeskChannel(HubLink):
    """The real channel's hub link (`bin/desk-channel` runs this class), plus
    a stop for the test's cleanup: the reconnect loop ends at its next
    attempt instead of retrying for the rest of the suite. Nothing the
    channel does while running is changed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stopped = threading.Event()

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    def _connect_and_serve(self) -> bool:
        if self._stopped.is_set():
            raise SystemExit  # ends the thread quietly (threading.excepthook skips it)
        return super()._connect_and_serve()


class HeldBackoff(Backoff):
    """The relay's backoff, recording each attempt's end; the next attempt
    starts at once unless `release` is cleared, then when it is set again."""

    def __init__(self):
        super().__init__(rng=lambda: 0.5)
        self.ends: list[Attempt] = []
        self.release = threading.Event()
        self.release.set()
        self._changed = threading.Condition()

    def after(self, attempt: Attempt) -> float:
        super().after(attempt)
        with self._changed:
            self.ends.append(attempt)
            self._changed.notify_all()
        self.release.wait(GENEROUS)
        return 0.05

    def wait_end(self, count: int = 1, timeout: float = GENEROUS) -> Attempt:
        deadline = time.monotonic() + timeout
        with self._changed:
            while len(self.ends) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"{len(self.ends)} of {count} link attempts ended in time")
                self._changed.wait(remaining)
            return self.ends[count - 1]


class JoinedCase(LinkHubCase, HubCase):
    """The hub (in `self.home`, the Pi), the real pump in place of ssh, the
    relay (`relay.serve` in `self.mac.home`, its kernel faked by `self.mac`)
    and the real channel, joined. `MAC_SESSION` is enrolled for `mac`.
    `start_relay` waits for the link to be up; `desk_channel` for the
    session's registration. `self.pump_stderr`, when set before
    `start_relay`, collects the pump's exit lines."""

    def setUp(self):
        super().setUp()
        self.mac = MacHome(self, MAC_SESSION)
        self.remote[MAC_SESSION] = MAC
        self.hub.refresh_files()
        self.backoff = HeldBackoff()
        self.relay_links: list[RelayLink] = []
        self.pump_stderr: Path | None = None
        self.stdin = ClaudeStdin()

    # -- the relay ------------------------------------------------------------------

    def sign(self, message: bytes, deadline_seconds: float) -> str:
        return fake_signature(self.key("mac").signer)

    def pump(self, config) -> SshInvocation:
        invocation = pump_invocation(self.home)
        if self.pump_stderr is None:
            return invocation
        return replace(invocation, argv=(sys.executable, "-I", "-c", _PUMP_STDERR_TO_FILE, str(self.pump_stderr),
                                         *invocation.argv))

    def start_relay(self, *, wait: bool = True, **link_options) -> None:
        def channels_factory(config):
            self.relay = RelayChannels(config, reader=self.mac.kernel)
            return self.relay

        def link_factory(*args, **kwargs):
            link = RelayLink(*args, **kwargs)
            self.relay_links.append(link)
            return link

        def connector_factory(config, channels, *, machine, relay_build):
            self.connector = RelayConnector(config, channels, machine=machine, relay_build=relay_build,
                                            prepare=self.pump, link_factory=link_factory, backoff=self.backoff,
                                            sign=self.sign, reader=self.mac.kernel, **link_options)
            return self.connector

        stop = threading.Event()
        self.relay_exit: list[int] = []
        thread = threading.Thread(target=lambda: self.relay_exit.append(
            serve_relay(self.mac.config, stop, machine=MACHINE, channels_factory=channels_factory,
                        connector_factory=connector_factory)), name="test-relay", daemon=True)
        thread.start()

        def stop_relay():
            self.backoff.release.set()
            stop.set()
            thread.join(GENEROUS)
            for link in self.relay_links:
                if link.child is not None:
                    link.child.reaped.wait(GENEROUS)
        self.addCleanup(stop_relay)
        self.stop_relay = stop_relay
        if wait:
            self.wait_link("up")

    def wait_link(self, state: str) -> dict:
        return wait_until(lambda: (self.links().get(MAC) or {}).get("state") == state and self.links()[MAC],
                          GENEROUS)

    def relay_pump(self) -> int:
        """The pid of the current link's pump (the relay's ssh child)."""
        return self.relay_links[-1].child.pid

    # -- the channel ----------------------------------------------------------------

    def desk_channel(self, writer=None, *, wait: bool = True, **options) -> DeskChannel:
        channel = DeskChannel(self.mac.config, MAC_SESSION, StdoutWriter(writer or self.stdin), **options)
        channel.start()
        self.addCleanup(channel.stop)
        if wait:
            wait_until(lambda: self.target(MAC_SESSION)["registration"] == "registered", GENEROUS)
        return channel

    # -- the hub's side ---------------------------------------------------------------

    def send_mac(self, text: str = "how is the build?") -> dict:
        target = self.target(MAC_SESSION)
        ticket = self.hub.ticket({"session_id": MAC_SESSION, "generation": target["generation"],
                                  "epoch": self.hub.epoch}, self.token)["ticket"]
        return self.hub.send({"ticket": ticket, "text": text}, self.token)

    def lock_mac(self) -> dict | None:
        return self.target(MAC_SESSION)["lock"]

    def clear_mac(self) -> dict:
        return self.hub.clear({"session_id": MAC_SESSION, "lock_id": self.lock_mac()["lock_id"]}, self.token)

    def request_view(self, request_id: str) -> dict:
        return next(r for r in self.hub.state_view()["requests"] if r["request_id"] == request_id)


__all__ = ["CHANNEL_PID", "CLAUDE_PID", "EPOCH", "FAKE_SIGNATURE", "FakeChannels", "FakeHub", "FakeKernel",
           "FakeUplink", "LINK_ID", "LinkFrameError", "MacHome", "fake_sign", "pump_invocation", "script_invocation",
           "Gate", "GatedPipe", "GatedSocket", "LinkCase", "Run", "MAC_SESSION", "ClaudeStdin", "DeskChannel",
           "HeldBackoff", "JoinedCase"]
