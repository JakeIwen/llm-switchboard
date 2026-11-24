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

from desk_fixtures import SESSION_A, Clock, desk_config, make_home, request_id
from link_peer import MACHINE
from llm_switchboard.desk.link_protocol import (
    LINK_VERSIONS, MAX_LINK_LINE_BYTES, VERSION_FREE, LineSplitter, LinkFrameError, LinkType, Side, decode_frame,
    encode_frame,
)
from llm_switchboard.desk.link_writer import PipeTarget, SocketTarget
from llm_switchboard.desk.mac_process import MacProcess, ProcessStamp
from llm_switchboard.desk.relay_channels import RelayRefusal
from llm_switchboard.desk.relay_link import Attempt, RelayLink
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

__all__ = ["CHANNEL_PID", "CLAUDE_PID", "EPOCH", "FAKE_SIGNATURE", "FakeChannels", "FakeHub", "FakeKernel",
           "FakeUplink", "LINK_ID", "LinkFrameError", "MacHome", "fake_sign", "pump_invocation", "script_invocation",
           "Gate", "GatedPipe", "GatedSocket", "LinkCase", "Run"]
