import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

from llm_switchboard.codex_client import CodexClient, CodexError
from llm_switchboard.codex_threads import CodexCatalog
from llm_switchboard.codex_trash import CodexTrash
from llm_switchboard.folders import new_session_folders
from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.trash import RETENTION_S, Trash
from llm_switchboard.web import make_app, make_server

from fake_codex import FakeCodexDaemon, thread
from fakes import SID, Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript

T1 = "01a13da1-a87b-76d0-bf7f-fbe83e9bc64b"
T2 = "01a13bb6-50b5-7990-89de-3660e987cc2a"
SUB = "01a13bb6-0000-7990-89de-3660e987cc2a"


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work = self.root / "work"
        self.other = self.root / "other"
        for d in (self.work, self.other):
            d.mkdir()
        self.daemon = FakeCodexDaemon(self.root / "codex.sock", [
            thread(T1, str(self.work), name="Find compass signal", preview="compass?"),
            thread(T2, str(self.work), preview="  untitled   first prompt "),
            thread(SUB, str(self.work), name="a subagent", parentThreadId=T1),
        ])
        self.addCleanup(self.daemon.close)
        self.store = Store(self.root / "sessions.json")
        self.index = FakeIndex(transcript(SID, "Claude one", str(self.work)))
        self.tmux = FakeTmux()
        self.clock = Clock()
        self.sup = Supervisor(self.store, self.index, self.tmux, FakeConnectivity(), registry=lambda: [], clock=self.clock)
        state = self.root / "claude.json"
        state.write_text(json.dumps({"projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        self.app = make_app(self.store, self.index, self.sup, self.tmux, Trash(self.root / "trash"), self.root / "codex.sock",
                            registry=lambda: [], codex_trash=CodexTrash(self.root / "codex-trash.json"),
                            folders=partial(new_session_folders, state_file=state, home=self.root / "home"))
        self.codex = self.app.codex
        self.codex.clock = self.clock

    def rows(self):
        return {r["id"]: r for r in self.app.list_sessions()["sessions"]}

    def methods(self):
        return [m for m, _ in self.daemon.calls if m not in ("initialize", "thread/list")]


class CatalogTest(Fixture):
    def test_lists_codex_threads_beside_claude_sessions(self):
        rows = self.rows()
        self.assertEqual(rows[SID]["tool"], "claude")
        self.assertEqual(rows[T1]["tool"], "codex")
        self.assertEqual(rows[T1]["title"], "Find compass signal")
        self.assertEqual(rows[T2]["title"], "untitled first prompt")
        self.assertNotIn(SUB, rows)
        self.assertIn(f"codexp resume {T1}", rows[T1]["resume_cmd"])
        self.assertEqual(self.app.list_sessions()["codex"], {"enabled": True, "error": ""})

    def test_unreachable_daemon_is_reported_not_fatal(self):
        self.daemon.close()
        self.codex.catalog.invalidate()
        listing = self.app.list_sessions()
        self.assertIn("isn't reachable", listing["codex"]["error"])
        self.assertIn(SID, {r["id"] for r in listing["sessions"]})

    def test_messages(self):
        self.daemon.threads[T1]["turns"] = [{"user": "where is north?", "agent": "Up."}]
        self.assertEqual([(m["role"], m["text"]) for m in self.app.messages(T1)["messages"]],
                         [("user", "where is north?"), ("assistant", "Up.")])

    def test_rpc_errors_raise(self):
        with self.assertRaises(CodexError):
            CodexClient(self.root / "codex.sock").call("no/such")

    def test_folders_count_codex_sessions(self):
        folders = {f["cwd"]: f for f in self.app.list_folders()["folders"]}
        self.assertEqual(folders[str(self.work)]["sessions"], 3)
        self.assertEqual(folders[str(self.work)]["codex_blocked"], "")


class ActionsTest(Fixture):
    def test_rename_and_unique_names_across_tools(self):
        self.app.act(T2, "rename", {"title": "  compass   two "})
        self.assertEqual(self.daemon.threads[T2]["name"], "compass two")
        self.assertEqual(self.rows()[T2]["title"], "compass two")
        for taken in ("claude ONE", "find compass signal"):
            with self.assertRaises(Exception) as e:
                self.app.act(T2, "rename", {"title": taken})
            self.assertIn("already has a session named", str(e.exception))
        with self.assertRaises(Exception):
            self.app.actions.rename(SID, "Compass two")

    def test_delete_archives_and_restore_unarchives(self):
        self.assertTrue(self.app.act(T1, "delete", {})["trashed"])
        self.assertNotIn(T1, self.rows())
        [item] = self.app.list_trash()["trash"]
        self.assertEqual((item["id"], item["tool"], item["title"]), (T1, "codex", "Find compass signal"))
        self.app.act(T1, "restore", {})
        self.assertIn(T1, self.rows())
        self.assertEqual(self.app.list_trash()["trash"], [])

    def test_delete_refused_while_working(self):
        self.daemon.threads[T1]["status"] = {"type": "active"}
        self.codex.catalog.invalidate()
        with self.assertRaises(Exception) as e:
            self.app.act(T1, "delete", {})
        self.assertIn("working", str(e.exception))

    def test_trash_expires_and_empties_for_good(self):
        self.app.act(T1, "delete", {})
        self.clock.t += RETENTION_S + 1
        self.assertEqual(self.app.list_trash()["trash"], [])
        self.assertNotIn(T1, self.daemon.threads)
        self.app.act(T2, "delete", {})
        self.assertEqual(self.app.empty_trash()["removed"], 1)
        self.assertNotIn(T2, self.daemon.threads)

    def test_fork_into_another_folder(self):
        made = self.app.act(T1, "fork", {"cwd": str(self.other), "title": "compass, take two"})
        new = self.daemon.threads[made["id"]]
        self.assertEqual((new["cwd"], new["name"], new["forkedFromId"]), (str(self.other), "compass, take two", T1))
        self.assertIn("thread/unsubscribe", self.methods())
        self.assertEqual(self.rows()[made["id"]]["project"], str(self.other))
        # A thread a fork points at can't be deleted for good; it stays archived in Codex.
        self.app.act(T1, "delete", {})
        self.app.empty_trash()
        self.assertTrue(self.daemon.threads[T1]["archived"])
        self.assertEqual(self.app.list_trash()["trash"], [])

    def test_fork_needs_a_free_name_and_no_move(self):
        with self.assertRaises(Exception):
            self.app.act(T1, "fork", {"cwd": "", "title": "Claude one"})
        with self.assertRaises(ValueError) as e:
            self.app.act(T1, "move", {"cwd": str(self.other)})
        self.assertIn("fork it", str(e.exception))

    def test_create_starts_a_named_thread_working_on_its_first_message(self):
        with self.assertRaises(ValueError):
            self.app.create({"tool": "codex", "cwd": str(self.work), "name": "logger", "prompt": "  "})
        made = self.app.create({"tool": "codex", "cwd": str(self.work), "name": "logger", "prompt": "add a logger"})
        self.assertEqual(self.methods(), ["thread/start", "thread/name/set", "turn/start"])
        start = next(p for m, p in self.daemon.calls if m == "thread/start")
        self.assertEqual(start["approvalPolicy"], "never")
        row = self.rows()[made["id"]]
        self.assertEqual((row["title"], row["codex_state"], row["first_prompt"]), ("logger", "active", "add a logger"))
        with self.assertRaises(Exception):
            self.app.create({"tool": "codex", "cwd": str(self.work), "name": "Logger", "prompt": "again"})


class WebTest(Fixture):
    def setUp(self):
        super().setUp()
        server = make_server(self.app, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_actions_route_to_codex_and_errors_map(self):
        self.assertEqual(self.post(f"/api/sessions/{T1}/rename", {"title": "new name"})[0], 200)
        self.assertEqual(self.post(f"/api/sessions/{T2}/rename", {"title": "New Name"})[0], 409)
        self.assertEqual(self.post(f"/api/sessions/{T1}/enable", {})[0], 400)
        self.daemon.close()
        self.codex.catalog.invalidate()
        status, body = self.post("/api/sessions/new", {"tool": "codex", "cwd": str(self.work), "name": "x", "prompt": "y"})
        self.assertEqual(status, 503, body)


if __name__ == "__main__":
    unittest.main()
