import json
import tempfile
import unittest
from pathlib import Path

from claude_always_remote.conversation import recent_messages
from claude_always_remote.live_registry import LiveProcess
from claude_always_remote.session_actions import Conflict, SessionActions
from claude_always_remote.store import Store
from claude_always_remote.supervisor import Supervisor
from claude_always_remote.transcripts import TranscriptIndex
from claude_always_remote.trash import RETENTION_S, Trash

from fakes import SID, Clock, FakeConnectivity, FakeTmux


def line(record):
    return json.dumps(record, separators=(",", ":")) + "\n"


class SessionActionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.path = root / "projects" / "-home-pi-dev-x" / f"{SID}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(line({"type": "user", "cwd": "/home/pi/dev/x", "message": {"content": "hello"}})
                             + line({"type": "bridge-session", "bridgeSessionId": "cse_01ABC"}))
        (self.path.parent / SID / "subagents").mkdir(parents=True)
        self.index = TranscriptIndex(root / "projects")
        self.store = Store(root / "sessions.json")
        self.tmux = FakeTmux()
        self.live: list[LiveProcess] = []
        self.clock = Clock()
        self.trash = Trash(root / "trash", clock=self.clock)
        self.sup = Supervisor(self.store, self.index, self.tmux, FakeConnectivity(),
                              registry=lambda: self.live, clock=self.clock)
        self.actions = SessionActions(self.store, self.index, self.sup, self.tmux, self.trash,
                                      registry=lambda: self.live, sleep=self._tick)

    def _tick(self, _):
        # Stand-in for waiting: let the supervisor run its pass and the process exit.
        self.sup.reconcile()
        self.live = [p for p in self.live if p.managed_for != SID]

    def proc(self, managed, status="idle"):
        return LiveProcess(pid=9, session_id=SID, cwd="", status=status, managed_for=SID if managed else None, rss_kb=1)

    def test_transcript_exposes_remote_url(self):
        self.assertEqual(self.index.get(SID).remote_url, "https://claude.ai/code/session_01ABC")

    def test_rename_closed_session_appends_custom_title(self):
        result = self.actions.rename(SID, "  van   telemetrics ")
        self.assertEqual(result["via"], "transcript")
        self.assertEqual(self.index.get(SID).title, "van telemetrics")

    def test_rename_running_session_types_slash_rename(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.live = [self.proc(managed=True)]
        self.assertEqual(self.actions.rename(SID, "new name")["via"], "running session")
        self.assertEqual(self.tmux.sent, [(self.tmux.window_for(SID).window_id, "/rename new name")])

    def test_rename_refused_while_busy_or_open_elsewhere(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.live = [self.proc(managed=True, status="busy")]
        with self.assertRaises(Conflict):
            self.actions.rename(SID, "x")
        self.live = [self.proc(managed=False)]
        with self.assertRaises(Conflict):
            self.actions.rename(SID, "x")
        self.assertEqual(self.tmux.sent, [])

    def test_rename_validates_title(self):
        for bad in ("", "   ", None, "x" * 121):
            with self.assertRaises(ValueError):
                self.actions.rename(SID, bad)

    def test_delete_stops_kept_session_and_restore_brings_it_back(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.live = [self.proc(managed=True)]
        self.actions.delete(SID)
        self.assertEqual(self.tmux.windows(), [])
        self.assertNotIn(SID, self.store.load())
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.index.get(SID))
        [item] = self.trash.items()
        self.assertEqual(item.title, "hello")
        self.actions.restore(SID)
        self.assertTrue(self.path.exists())
        self.assertTrue((self.path.parent / SID / "subagents").is_dir())
        self.assertEqual(self.trash.items(), [])

    def test_delete_refused_while_open_elsewhere(self):
        self.live = [self.proc(managed=False)]
        with self.assertRaises(Conflict):
            self.actions.delete(SID)
        self.assertTrue(self.path.exists())

    def test_restore_refuses_to_overwrite(self):
        self.actions.delete(SID)
        self.path.write_text("new")
        with self.assertRaises(Conflict):
            self.actions.restore(SID)

    def test_old_trash_expires(self):
        self.actions.delete(SID)
        self.clock.t += RETENTION_S + 1
        self.assertEqual(self.trash.items(), [])


class RecentMessagesTest(unittest.TestCase):
    def test_keeps_prompts_and_replies_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(
                line({"type": "user", "message": {"content": "fix the bug"}, "timestamp": "t1"})
                + line({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hm"}]}})
                + line({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
                + line({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}})
                + line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed it."}]}})
            )
            self.assertEqual(
                [(m["role"], m["text"]) for m in recent_messages(path)],
                [("user", "fix the bug"), ("assistant", "Fixed it.")],
            )


if __name__ == "__main__":
    unittest.main()
