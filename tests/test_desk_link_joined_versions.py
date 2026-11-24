"""The Mac link end to end, for mixed versions and privacy: a phase 1
channel through the new relay, a channel with no relay and no key, and what
a whole desk exchange leaves in logs, files and v1's frames
(docs/voice-desk.md, "Mac link", Tests: Mixed versions, Privacy)."""

import contextlib
import io
import json
import logging
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import types
import unittest
from unittest import mock

from desk_fixtures import make_home, wait_until
from desk_phase1 import phase1_root
from link_peer import MAC
from relay_fixture import CHANNEL_PID, GENEROUS, MAC_SESSION, ClaudeStdin, JoinedCase, MacHome
from llm_switchboard.codex_trash import CodexTrash
from llm_switchboard.desk import channel
from llm_switchboard.desk.channel import HubLink, StdoutWriter
from llm_switchboard.host_agent import HostAgent
from llm_switchboard.host_protocol import VERSION
from llm_switchboard.mac_action_runner import MacActionRunner
from llm_switchboard.mac_actions import MacActions
from llm_switchboard.mac_catalog import MacCatalog
from llm_switchboard.mac_uploads import MacUploads
from llm_switchboard.transcripts import TranscriptIndex, one_line, prompt_text, reply_text
from llm_switchboard.trash import Trash

REPO = Path(__file__).resolve().parent.parent
# A transcript whose first user record is the one Claude Code wrote for a
# real desk event, captured from a desk session and scrubbed (see the test).
DESK_EVENT_TRANSCRIPT = REPO / "tests" / "fixtures" / "desk-event-transcript.jsonl"
MARKER = "quokka-marker-5be1d07"

# The phase 1 channel's hub link in its own interpreter: events go to its
# stdout as Claude would read them; it runs until its stdin closes.
_PHASE1_CHANNEL = """
import sys
from llm_switchboard.desk.channel import HubLink, StdoutWriter
from llm_switchboard.desk.config import DeskConfig
HubLink(DeskConfig.from_env(), sys.argv[1], StdoutWriter(sys.stdout.buffer)).start()
sys.stdin.read()
"""


def child_env(home: Path) -> dict:
    return {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "XDG_CONFIG_HOME": "",
            "XDG_STATE_HOME": "", "XDG_RUNTIME_DIR": ""}


def read_line_within(stream, timeout: float) -> bytes:
    """One line from a child's pipe, or b"" when none comes in time."""
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True).start()
    try:
        return lines.get(timeout=timeout)
    except queue.Empty:
        return b""


class MixedVersionsTest(JoinedCase):
    def test_phase1_channel_through_new_relay(self):
        """The phase 1 channel (commit d024217) registers through the new
        relay, takes an event and reports it."""
        root = phase1_root(self)
        self.start_relay()
        # Its own final check reads its parent's session file: this process.
        guard = self.mac.config.claude_sessions_dir / f"{os.getpid()}.json"
        guard.write_text(json.dumps({"sessionId": MAC_SESSION, "pid": os.getpid()}))
        prelude = f"import sys; sys.path.insert(0, {str(root)!r})\n"
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", prelude + _PHASE1_CHANNEL, MAC_SESSION],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=child_env(self.mac.home))
        self.addCleanup(self._end_child, child)
        wait_until(lambda: self.target(MAC_SESSION)["registration"] == "registered", GENEROUS)
        sent = self.send_mac("from the new hub")
        line = read_line_within(child.stdout, GENEROUS)
        event = json.loads(line)["params"]
        self.assertEqual(event["meta"], {"request_id": sent["request_id"]})
        self.assertTrue(event["content"].startswith("from the new hub"))
        # Its `delivered` reached the hub: once the channel is gone the lock
        # clears with no presence check, though the Mac says it still runs.
        self._end_child(child)
        wait_until(lambda: self.hub.state.request_state(sent["request_id"]) == "unknown", GENEROUS)
        self.assertIn(CHANNEL_PID, self.mac.kernel.records)
        self.assertEqual(self.clear_mac(), {"cleared": True})

    @staticmethod
    def _end_child(child: subprocess.Popen) -> None:
        with contextlib.suppress(OSError):
            child.stdin.close()
        try:
            child.wait(GENEROUS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(GENEROUS)
        child.stdout.close()


class _Stop(Exception):
    pass


class ChannelWithoutRelayTest(unittest.TestCase):
    def test_live_channel_with_relay_and_key_absent(self):
        """A running channel on a Mac with no relay and no desk key: it
        retries on its backoff, one bounded log line an attempt, and nothing
        reaches Claude."""
        for stale_socket in (False, True):
            with self.subTest(stale_socket=stale_socket):
                mac = MacHome(self, MAC_SESSION)
                self.assertFalse((mac.home / ".ssh").exists())
                if stale_socket:  # a relay that died left its socket file
                    mac.config.channel_socket.parent.mkdir(parents=True)
                    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    listener.bind(str(mac.config.channel_socket))
                    listener.close()
                stdin = ClaudeStdin()
                link = HubLink(mac.config, MAC_SESSION, StdoutWriter(stdin))
                delays, lines = self._retries(link, attempts=9)
                self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30, 30, 30])
                self.assertEqual(channel.BACKOFF_MAX_SECONDS, 30)
                error = "ConnectionRefusedError" if stale_socket else "FileNotFoundError"
                self.assertEqual(lines, [f"hub link ended: {error}"] * 9)
                self.assertEqual(stdin.lines, [])

    def _retries(self, link: HubLink, attempts: int) -> tuple[list, list]:
        """The channel's reconnect loop on this thread, its sleeps recorded
        instead of slept, until `attempts` attempts have ended."""
        me = threading.current_thread()
        delays, lines = [], []
        real_log = channel.log

        def sleep(seconds):
            if threading.current_thread() is not me:  # another test's channel thread
                return channel_time.sleep(seconds)
            delays.append(seconds)
            if len(delays) == attempts:
                raise _Stop

        def log(message):
            if threading.current_thread() is me:
                lines.append(message)
            else:
                real_log(message)
        channel_time = channel.time
        fake_time = types.SimpleNamespace(sleep=sleep, monotonic=channel_time.monotonic)
        with mock.patch.object(channel, "time", fake_time), mock.patch.object(channel, "log", log), \
                self.assertRaises(_Stop):
            link._run()
        return delays, lines


class _Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.NOTSET)
        self.text = io.StringIO()
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record):
        self.text.write(self.format(record) + "\n")


class PrivacyTest(JoinedCase):
    def capture_logs(self) -> _Captured:
        """Every log record of this process (hub and relay), at every level."""
        handler = _Captured()
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)
        return handler

    def exchange(self, prompt: str, reply: str) -> dict:
        desk = self.desk_channel()
        sent = self.send_mac(prompt)
        self.assertIn(prompt, self.stdin.wait_event()["content"])
        self.assertIsNone(desk.send_reply(sent["request_id"], reply, reply))
        self.assertEqual(self.request_view(sent["request_id"])["reply"]["text"], reply)
        return sent

    def test_marker_absent_from_desk_logs_and_files(self):
        logs = self.capture_logs()
        self.pump_stderr = make_home(self) / "pump-stderr"
        channel_stderr = io.StringIO()
        with contextlib.redirect_stderr(channel_stderr):
            self.start_relay()
            self.exchange(f"please look at {MARKER} now", f"looked at {MARKER}")
            # The hub ends the link, so the pump writes its exit line.
            self.backoff.release.clear()
            self.hub.links.retire(self.hub.links.live_link(MAC), "test over")
        wait_until(lambda: self.pump_stderr.exists() and self.pump_stderr.read_text().strip(), GENEROUS)
        captured = {"logs": logs.text.getvalue(), "pump": self.pump_stderr.read_text(),
                    "channel": channel_stderr.getvalue()}
        self.assertIn("desk relay", captured["logs"])  # the relay's log lines were captured
        self.assertIn("handed off", captured["logs"])  # and the hub's
        self.assertIn("desk-link: ", captured["pump"])
        for name, text in captured.items():
            with self.subTest(log=name):
                self.assertNotIn(MARKER, text)
        named = [self.config.lock_file, self.config.remote_enrolled_file, self.config.link_signers_file,
                 self.mac.config.relay_launch_log, self.mac.config.relay_log]
        files = {path for home in (self.home, self.mac.home) for path in home.rglob("*") if path.is_file()}
        self.assertTrue({self.config.lock_file, self.config.remote_enrolled_file,
                         self.config.link_signers_file} <= files)
        for path in sorted(files | {p for p in named if p.exists()}):
            with self.subTest(file=str(path)):
                self.assertNotIn(MARKER.encode(), path.read_bytes())

    def test_desk_adds_no_v1_frame_fields(self):
        """v1's push for a Mac session is the same before and after a desk
        exchange with it."""
        projects = self.mac.home / ".claude" / "projects"
        transcript = projects / "-Users-me-dev-project" / f"{MAC_SESSION}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "user", "sessionId": MAC_SESSION, "cwd": "/Users/me/dev/project",
                                          "message": {"content": "start the build"}},
                                         separators=(",", ":")) + "\n")
        state = make_home(self)
        codex = mock.Mock(error="offline")
        codex.threads.return_value = []
        catalog = MacCatalog(TranscriptIndex(projects, metadata_only=True), codex, state / "codex",
                             registry=lambda: [])
        executor = MacActions(catalog, Trash(state / "trash"), CodexTrash(state / "codex-trash.json"),
                              MacUploads(state / "uploads.json", state / "logs", run=mock.Mock()))
        pushed = []

        def ssh(argv, *, input, **kwargs):
            pushed.append(json.loads(input))
            return types.SimpleNamespace(stdout=json.dumps({"version": VERSION, "actions": []}))
        agent = HostAgent("pi", MAC, state / "key", catalog=catalog, run=ssh, executor=executor,
                          runner=MacActionRunner(executor, state / "ledger.json"))
        agent.cycle()
        self.start_relay()
        self.exchange(f"desk prompt {MARKER}", f"desk reply {MARKER}")
        agent.cycle()
        before, after = pushed
        [row] = [r for r in before["rows"] if r["id"] == MAC_SESSION]
        self.assertEqual(row["title"], "start the build")
        self.assertEqual(after, before)
        self.assertNotIn(MARKER, json.dumps(after))


class FirstPromptTitleTest(unittest.TestCase):
    def test_desk_first_prompt_title_follows_v1_rule(self):
        """The v1 row's title for a transcript whose first user record is a
        desk event is whatever `prompt_text` and v1's title rule make of it,
        and the row carries no other prompt or reply text."""
        if not DESK_EVENT_TRANSCRIPT.is_file():
            self.skipTest(f"no fixture captured from a live desk session yet: {DESK_EVENT_TRANSCRIPT.name}")
        records = [json.loads(line) for line in DESK_EVENT_TRANSCRIPT.read_text().splitlines() if line.strip()]
        session_id = next(r["sessionId"] for r in records if r.get("sessionId"))
        home = make_home(self)
        projects = home / "projects"
        (projects / "-Users-me-dev-project").mkdir(parents=True)
        (projects / "-Users-me-dev-project" / f"{session_id}.jsonl").write_bytes(DESK_EVENT_TRANSCRIPT.read_bytes())
        prompts = [prompt_text(r) for r in records if r.get("type") == "user"]
        named = [str(r.get(key) or "") for key in ("customTitle", "aiTitle", "summary") for r in records]
        title = next((t for t in reversed(named) if t), "") or next((p for p in prompts if p), "")
        codex = mock.Mock(error="offline")
        codex.threads.return_value = []
        rows = MacCatalog(TranscriptIndex(projects, metadata_only=True), codex, home / "codex",
                          registry=lambda: []).rows()
        if not title:
            self.assertEqual(rows, [])
            return
        [row] = rows
        self.assertEqual(row["title"], one_line(title))
        replies = [reply_text(r) for r in records if r.get("type") == "assistant"]
        for field, value in row.items():
            if field == "title":
                continue
            for text in filter(None, prompts + replies):
                self.assertNotIn(text, str(value), field)

if __name__ == "__main__":
    unittest.main()
