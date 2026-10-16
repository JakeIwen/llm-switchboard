import json
import tempfile
import unittest
from pathlib import Path

from llm_switchboard.conversation import recent_messages
from llm_switchboard.live_registry import LiveProcess
from llm_switchboard.relocate import project_dir_name
from llm_switchboard.session_actions import Conflict, SessionActions
from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.transcripts import TranscriptIndex
from llm_switchboard.trash import RETENTION_S, Trash

from fakes import SID, Clock, FakeConnectivity, FakeTmux


def line(record):
    return json.dumps(record, separators=(",", ":")) + "\n"


class Fixture(unittest.TestCase):
    """One transcript whose working folder is a real temp directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.work = root / "work"
        self.work.mkdir()
        self.path = root / "projects" / project_dir_name(str(self.work)) / f"{SID}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(line({"type": "user", "cwd": str(self.work), "message": {"content": "hello"}})
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

    def target(self):
        """Another existing folder to move or fork into."""
        target = Path(self.trash.root).parent / "elsewhere"
        target.mkdir(exist_ok=True)
        return target

    def proc(self, managed, status="idle"):
        return LiveProcess(pid=9, session_id=SID, cwd="", status=status, managed_for=SID if managed else None, rss_kb=1)


class SessionActionsTest(Fixture):
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

    def test_empty_trash(self):
        self.actions.delete(SID)
        self.assertEqual(self.trash.empty(), 1)
        self.assertEqual(self.trash.items(), [])

    def test_old_trash_expires(self):
        self.actions.delete(SID)
        self.clock.t += RETENTION_S + 1
        self.assertEqual(self.trash.items(), [])


class MoveTest(Fixture):
    def test_move_rewrites_cwd_and_files_under_new_folder(self):
        before = self.path.read_bytes()
        target = self.target()
        result = self.actions.move(SID, str(target))
        self.assertTrue(result["moved"])
        new_path = self.index.projects_dir / project_dir_name(str(target)) / f"{SID}.jsonl"
        self.assertFalse(self.path.exists())
        self.assertTrue((new_path.parent / SID / "subagents").is_dir())
        self.assertEqual(new_path.read_bytes(),
                         before.replace(f'"cwd":"{self.work}"'.encode(), f'"cwd":"{target}"'.encode()))
        self.assertEqual(self.index.get(SID).cwd, str(target))

    def test_move_kept_session_stops_it_and_keeps_it_enabled(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.live = [self.proc(managed=True)]
        first = self.tmux.window_for(SID).window_id
        self.actions.move(SID, str(self.target()))
        self.assertIn(first, self.tmux.killed)
        self.assertTrue(self.store.load()[SID].enabled)
        self.sup.reconcile()
        [_, (_, command, _)] = self.tmux.started
        self.assertIn(str(self.target()), command)

    def test_move_validation(self):
        with self.assertRaises(ValueError):
            self.actions.move(SID, "relative/path")
        with self.assertRaises(ValueError):
            self.actions.move(SID, "/definitely/not/here")
        self.live = [self.proc(managed=False)]
        with self.assertRaises(Conflict):
            self.actions.move(SID, str(self.target()))

    def test_move_to_same_folder_is_a_no_op(self):
        self.assertFalse(self.actions.move(SID, f"{self.work}/")["moved"])
        self.assertTrue(self.path.exists())

    def test_folder_names_match_claude_code(self):
        self.assertEqual(project_dir_name("/home/pi/dev/obd-things/projects/vehicle_data/dashboard"),
                         "-home-pi-dev-obd-things-projects-vehicle-data-dashboard")


class ForkTest(Fixture):
    def test_fork_copies_the_conversation_under_a_new_id_and_name(self):
        with open(self.path, "a") as f:
            f.write(line({"type": "assistant", "sessionId": SID, "cwd": str(self.work), "message": {"content": []}}))
            f.write('{"type":"user","sessionId":"' + SID)  # still being written
        before = self.path.read_bytes()
        new = self.actions.fork(SID, "", "  the   copy ", keep=False)
        self.assertEqual(new["cwd"], str(self.work))
        self.assertEqual(self.path.read_bytes(), before)
        copy = self.index.get(new["id"])
        self.assertEqual(copy.path.parent, self.path.parent)
        self.assertEqual((copy.title, copy.cwd, copy.remote_url), ("the copy", str(self.work), ""))
        records = [json.loads(x) for x in copy.path.read_text().splitlines()]
        self.assertEqual([r["type"] for r in records], ["user", "assistant", "custom-title"])
        self.assertEqual(records[1]["sessionId"], new["id"])
        self.assertNotIn(SID, copy.path.read_text())
        self.assertNotIn(new["id"], self.store.load())

    def test_fork_elsewhere_and_keep_running_resumes_the_copy_there(self):
        target = self.target()
        new = self.actions.fork(SID, str(target), "copy", keep=True)
        self.assertEqual(self.index.get(new["id"]).cwd, str(target))
        self.assertTrue(self.store.load()[new["id"]].enabled)
        self.sup.reconcile()
        [(sid, command, _)] = self.tmux.started
        self.assertEqual(sid, new["id"])
        self.assertIn(f"--resume {new['id']}", command)
        self.assertIn(str(target), command)

    def test_names_are_unique_per_folder(self):
        with self.assertRaises(Conflict):
            self.actions.fork(SID, "", " HELLO ")
        self.actions.fork(SID, "", "copy")
        with self.assertRaises(Conflict):
            self.actions.rename(SID, "Copy")
        self.actions.rename(SID, "Hello")  # its own name, recased
        self.actions.fork(SID, str(self.target()), "hello")
        with self.assertRaises(Conflict):
            self.actions.move(SID, str(self.target()))
        self.assertTrue(self.path.exists())


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
