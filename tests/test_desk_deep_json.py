"""Readers of JSON another process writes (Claude's session files and
transcripts, the lock file, channel frames) treat deeply nested content
like any other malformed content, never raising RecursionError out of the
reader. Each reader runs in a thread with a small stack, so nesting that
fits the readers' size caps overflows the parser on every Python version
(3.11 counts recursion; newer versions measure the C stack)."""

import ast
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

from desk_fixtures import SESSION_A, SESSION_B, desk_config, make_home

from llm_switchboard.desk.channel import McpServer
from llm_switchboard.desk.enrollment import Enrollment
from llm_switchboard.desk.labels import SessionLabels, read_title_and_cwd
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import LockStoreError
from llm_switchboard.desk.peer import read_session_file
from llm_switchboard.desk.protocol import FrameError, decode
from llm_switchboard.desk.startup_registry import read_session_entry

_SMALL_STACK = 256 * 1024
# Fits the 64 KiB caps of the session entry and the lock file.
DEEP = "[" * 30000 + "]" * 30000


def in_small_stack(fn):
    """fn's result, or the exception it raised, from a small-stack thread."""
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            out["error"] = exc
    old = threading.stack_size(_SMALL_STACK)
    try:
        thread = threading.Thread(target=run)
        thread.start()
    finally:
        threading.stack_size(old)
    thread.join()
    if "error" in out:
        raise out["error"]
    return out["value"]


class DeepNestingTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    # Shows the harness reaches the overflow the readers must never meet.
    # CPython 3.13 neither counts the parser's C recursion against the
    # recursion limit (3.11 does) nor measures the C stack (3.14 does), so on
    # it the overflow is a SIGBUS that kills the whole test run: that crash is
    # why every reader goes through shallow_json, and why only this test,
    # which calls json.loads directly, is skipped there.
    @unittest.skipIf(sys.version_info[:2] == (3, 13),
                     "CPython 3.13 crashes the process on a thread stack overflow instead of raising RecursionError")
    def test_the_parser_really_overflows_in_this_harness(self):
        with self.assertRaises(RecursionError):
            in_small_stack(lambda: json.loads(DEEP))

    def test_claudes_session_entry_reads_as_naming_no_session(self):
        (self.dir / "42.json").write_text(DEEP)
        self.assertEqual(in_small_stack(lambda: read_session_entry(self.dir, 42)), (True, None))
        self.assertIsNone(in_small_stack(lambda: read_session_file(self.dir, 42)))

    def test_a_label_falls_back_past_a_deep_transcript_line_and_session_file(self):
        config = desk_config(make_home(self))
        transcript = config.home / ".claude" / "projects" / "p" / f"{SESSION_A}.jsonl"
        transcript.parent.mkdir(parents=True)
        nested_summary = "[" * 30000 + '{"type":"summary","summary":"lost"}' + "]" * 30000
        transcript.write_text(nested_summary + "\n" + '{"type":"summary","summary":"kept"}\n')
        self.assertEqual(in_small_stack(lambda: read_title_and_cwd(transcript, transcript.stat().st_size))[0],
                         "kept")
        config.claude_sessions_dir.mkdir(parents=True)
        (config.claude_sessions_dir / "42.json").write_text(DEEP)
        labels = SessionLabels(config)
        self.assertEqual(in_small_stack(lambda: labels.describe(SESSION_B, 42)), (SESSION_B[:8], ""))

    def test_the_lock_file_reads_as_corrupt(self):
        path = self.dir / "locks.json"
        path.write_text(DEEP)
        with self.assertRaises(LockStoreError):
            in_small_stack(LockStore(path).load)

    def test_a_channel_frame_is_a_frame_error(self):
        with self.assertRaises(FrameError):
            in_small_stack(lambda: decode(("[" * 3000 + "]" * 3000).encode()))

    def test_the_enrollment_file_reads_as_unreadable(self):
        path = self.dir / "enrolled.json"
        path.write_text('{"version":1,"sessions":' + DEEP + "}")
        self.assertEqual(in_small_stack(Enrollment(path).sessions), {})

    def test_a_deep_mcp_line_from_claude_is_ignored(self):
        # A line the server can't parse is dropped before the link or the
        # writer is touched.
        server = McpServer(None, None)
        self.assertIsNone(in_small_stack(lambda: server.handle_line(DEEP.encode())))


DESK_DIR = Path(__file__).resolve().parent.parent / "llm_switchboard" / "desk"


class OnlyShallowJsonParsesTest(unittest.TestCase):
    """Every desk reader parses through shallow_json, so none relies on
    RecursionError (which CPython 3.13 doesn't raise in a small-stack thread)."""

    def test_no_desk_module_calls_the_json_parser_directly(self):
        offenders = []
        for path in sorted(DESK_DIR.rglob("*.py")):
            if path.name == "shallow_json.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "json" and any(
                        alias.name in {"load", "loads", "JSONDecoder"} for alias in node.names):
                    offenders.append(f"{path.name}:{node.lineno} imports the parser from json")
                if (isinstance(node, ast.Attribute) and node.attr in {"load", "loads", "JSONDecoder"}
                        and isinstance(node.value, ast.Name) and node.value.id == "json"):
                    offenders.append(f"{path.name}:{node.lineno} json.{node.attr}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
