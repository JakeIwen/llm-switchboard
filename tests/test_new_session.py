"""Creating sessions from the picker: allowed folders, the launch path before a
transcript exists, ready notifications, and deleting one that never started."""

import json
import shlex
import tempfile
import time
import unittest
from pathlib import Path

from claude_always_remote.catalog import session_rows
from claude_always_remote.folders import new_session_folders
from claude_always_remote.live_registry import LiveProcess
from claude_always_remote.notify import Notifier
from claude_always_remote.session_actions import Conflict, SessionActions
from claude_always_remote.store import Store
from claude_always_remote.supervisor import RC_GRACE_S, State, Supervisor
from claude_always_remote.trash import Trash

from fakes import Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript

SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__(Path("/dev/null"))
        self.sent = []

    def send(self, title, message, priority="default", tags=""):
        self.sent.append((title, message, priority))


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home = root / "home"
        self.trusted = self.home / "dev" / "proj"
        self.untrusted = root / "elsewhere"
        for d in (self.trusted, self.untrusted):
            d.mkdir(parents=True)
        self.state_file = root / "claude.json"
        self.state_file.write_text(json.dumps({"projects": {
            str(self.home / "dev"): {"hasTrustDialogAccepted": True},
            str(self.home): {"hasTrustDialogAccepted": True},
        }}))
        self.index = FakeIndex(
            transcript(SID_A, "in proj", str(self.trusted)),
            transcript(SID_B, "untrusted", str(self.untrusted)),
            transcript("cccccccc-0000-4000-8000-000000000003", "home", str(self.home)),
            transcript("dddddddd-0000-4000-8000-000000000004", "gone", str(root / "deleted")),
        )
        self.store = Store(root / "sessions.json")
        self.tmux = FakeTmux()
        self.clock = Clock()
        self.notifier = RecordingNotifier()
        self.live: list[LiveProcess] = []
        self.sup = Supervisor(self.store, self.index, self.tmux, FakeConnectivity(), registry=lambda: self.live,
                              clock=self.clock, notifier=self.notifier)
        self.actions = SessionActions(self.store, self.index, self.sup, self.tmux, Trash(root / "trash"),
                                      registry=lambda: self.live, sleep=lambda _: self.sup.reconcile(),
                                      folders=lambda ts: new_session_folders(ts, self.state_file, self.home))

    def folders(self):
        return {f.cwd: f.blocked for f in new_session_folders(self.index.all(), self.state_file, self.home)}


class FoldersTest(Fixture):
    def test_only_existing_trusted_non_home_folders_are_open(self):
        folders = self.folders()
        self.assertEqual(folders[str(self.trusted)], "")
        self.assertIn("Not trusted", folders[str(self.untrusted)])
        self.assertIn("~", folders[str(self.home)])
        self.assertIn("no longer exists", [v for k, v in folders.items() if k.endswith("deleted")][0])

    def test_unreadable_state_file_trusts_nothing(self):
        self.state_file.write_text("{torn")
        self.assertIn("Not trusted", self.folders()[str(self.trusted)])


class CreateTest(Fixture):
    def create(self, cwd, name):
        return self.actions.create(cwd, name)

    def test_create_launches_a_fresh_session_with_its_name(self):
        sid = self.create(str(self.trusted), "  van   logger ")["id"]
        entry = self.store.load()[sid]
        self.assertTrue(entry.enabled)
        self.assertEqual((entry.cwd, entry.title), (str(self.trusted), "van logger"))
        self.sup.reconcile()
        argv = shlex.split(shlex.split(self.tmux.started[0][1])[3])
        self.assertEqual(argv[:3], ["cd", "--", str(self.trusted)])
        self.assertEqual(argv[argv.index("--session-id") + 1], sid)
        self.assertEqual(argv[argv.index("--name") + 1], "van logger")
        self.assertEqual(argv[argv.index("--remote-control") + 1], "van logger")
        self.assertNotIn("--resume", argv)

    def test_after_claude_writes_the_transcript_restarts_resume(self):
        sid = self.create(str(self.trusted), "logger")["id"]
        self.sup.reconcile()
        self.index.items[sid] = transcript(sid, "logger", str(self.trusted))
        self.tmux.exit(sid, 1)
        self.sup.reconcile()
        self.clock.t = self.sup.snapshot()[sid].next_attempt
        self.sup.reconcile()
        self.assertIn("--resume", self.tmux.started[1][1])

    def test_create_rejects_unlisted_blocked_and_unnamed(self):
        with self.assertRaises(ValueError):
            self.create("/somewhere/new", "x")
        with self.assertRaises(Conflict):
            self.create(str(self.untrusted), "x")
        with self.assertRaises(Conflict):
            self.create(str(self.home), "x")
        with self.assertRaises(ValueError):
            self.create(str(self.trusted), "   ")
        self.assertEqual(self.store.load(), {})

    def test_ready_is_announced_once(self):
        sid = self.create(str(self.trusted), "logger")["id"]
        self.sup.reconcile()
        self.assertEqual(self.notifier.sent, [])
        self.tmux.screens[self.tmux.window_for(sid).window_id] = "status line   /rc"
        self.sup.reconcile()
        self.sup.reconcile()
        [(title, _, priority)] = self.notifier.sent
        self.assertEqual((title, priority), ("Claude session ready: logger", "default"))

    def test_problem_is_announced(self):
        sid = self.create(str(self.trusted), "logger")["id"]
        self.sup.reconcile()
        self.clock.t += RC_GRACE_S + 1
        self.sup.reconcile()
        [(title, _, priority)] = self.notifier.sent
        self.assertEqual((title, priority), ("Claude session needs attention: logger", "high"))

    def test_pending_row_and_delete_without_transcript(self):
        sid = self.create(str(self.trusted), "logger")["id"]
        self.sup.reconcile()
        row = next(r for r in session_rows(self.index.all(), self.store.load(), [], self.sup.snapshot()) if r["id"] == sid)
        self.assertTrue(row["new"])
        self.assertEqual((row["title"], row["cwd"]), ("logger", str(self.trusted)))
        self.assertFalse(self.actions.delete(sid)["trashed"])
        self.assertEqual(self.tmux.windows(), [])
        self.assertNotIn(sid, self.store.load())


class NotifierTest(unittest.TestCase):
    def test_runs_script_with_title_message_priority_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "args"
            script = Path(tmp) / "notify.sh"
            script.write_text(f'printf "%s\\n" "$@" > {out}\n')
            Notifier(script).send("Title here", "a message", "high", "warning")
            for _ in range(50):
                if out.exists():
                    break
                time.sleep(0.05)
            self.assertEqual(out.read_text().splitlines(), ["Title here", "a message", "high", "warning"])

    def test_disabled_notifier_does_nothing(self):
        self.assertFalse(Notifier(None).enabled)
        Notifier(None).send("x", "y")


if __name__ == "__main__":
    unittest.main()
