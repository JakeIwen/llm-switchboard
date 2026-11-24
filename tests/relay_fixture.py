"""The Mac relay's surroundings for tests: an isolated Mac home whose kernel
facts come from a fake libproc reader (so the tests run on Linux too), and
`FakeUplink`, the hub's end of a link as `relay_channels` sees it, which
records every frame the relay sends after checking it the way the hub's
decoder would.

`MacHome` builds the same launch `tests/test_desk_peer_mac.py` checks (the
framework interpreter, an installed claude, a session file matching the
parent's start); it is written again here rather than imported from that
test module, which has no shared fixture.
"""

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

from desk_fixtures import SESSION_A, desk_config, make_home
from llm_switchboard.desk.link_protocol import LINK_VERSIONS, LinkFrameError, LinkType, Side, decode_frame, encode_frame
from llm_switchboard.desk.mac_process import MacProcess, ProcessStamp

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


__all__ = ["CHANNEL_PID", "CLAUDE_PID", "EPOCH", "FakeKernel", "FakeUplink", "LinkFrameError", "MacHome"]
