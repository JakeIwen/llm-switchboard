import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from llm_switchboard import settings
from llm_switchboard.hosts import Hosts
from llm_switchboard.codex_trash import CodexTrash
from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.trash import Trash
from llm_switchboard.web import App, make_app, make_server

from fake_codex import FakeCodexDaemon, thread
from fakes import SID, SID2, Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript


class WebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "sessions.json")
        index = FakeIndex(transcript())
        self.tmux = FakeTmux()
        self.sup = Supervisor(self.store, index, self.tmux, FakeConnectivity(), registry=lambda: [], clock=Clock())
        self.root = Path(tmp.name)
        self.app = App(self.store, index, self.sup, self.tmux, Trash(self.root / "trash"), registry=lambda: [],
                       settings_file=self.root / "settings.json")
        server = make_server(self.app, "127.0.0.1", 0, strict_responses=True)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def call(self, path, body=None, content_type="application/json"):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_enable_then_list_shows_supervisor_state(self):
        self.assertEqual(self.call(f"/api/sessions/{SID}/enable", {})[0], 200)
        self.assertTrue(self.store.load()[SID].enabled)
        self.sup.reconcile()
        status, body = self.call("/api/sessions")
        [row] = body["sessions"]
        self.assertTrue(row["enabled"])
        self.assertEqual(row["status"]["state"], "starting")

    def test_post_must_be_json(self):
        status, _ = self.call(f"/api/sessions/{SID}/enable", {}, content_type="text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(self.store.load(), {})

    def test_rejects_unknown_and_malformed_ids(self):
        self.assertEqual(self.call(f"/api/sessions/{SID2}/enable", {})[0], 404)
        self.assertEqual(self.call("/api/sessions/nope/enable", {})[0], 400)

    def test_name_and_screen(self):
        self.assertEqual(self.call(f"/api/sessions/{SID}/name", {"name": "van"})[1]["rc_name"], "van")
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.tmux.screens[self.tmux.window_for(SID).window_id] = "hello screen"
        status, body = self.call(f"/api/sessions/{SID}/screen")
        self.assertEqual(body, {"source": "live", "screen": "hello screen"})

    def test_unknown_actions_are_not_found(self):
        self.assertEqual(self.call(f"/api/sessions/{SID}/explode", {})[0], 404)
        self.assertEqual(self.call(f"/api/sessions/{SID}/screen", {})[0], 404)

    def test_rename_conflict_maps_to_409(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()  # window up, but no idle claude process yet
        status, body = self.call(f"/api/sessions/{SID}/rename", {"title": "new"})
        self.assertEqual(status, 409)
        self.assertIn("idle", body["error"])

    def test_settings_read_partial_update_and_session_payload(self):
        status, payload = self.call("/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"settings": settings.defaults(), "defaults": settings.defaults()})
        status, payload = self.call("/api/settings", {"claude_command": "claudep", "folders": {"depth": 4}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["folders"], {"depth": 4, "min_sessions": 2, "hidden": []})
        self.assertEqual(payload["defaults"], settings.defaults())
        self.assertEqual(self.call("/api/sessions")[1]["settings"], payload["settings"])
        self.assertEqual(self.call("/api/settings")[1], payload)
        settings.save({"poll_seconds": 8}, self.app.settings_file)
        self.assertEqual(self.call("/api/sessions")[1]["settings"]["poll_seconds"], 8)

    def test_settings_bad_values_are_400_and_json_is_required(self):
        for update, key in [({"claude_command": "echo;true"}, "claude_command"),
                            ({"poll_seconds": 0}, "poll_seconds"), ({"folders": {"depth": 99}}, "folders.depth")]:
            with self.subTest(update=update):
                status, payload = self.call("/api/settings", update)
                self.assertEqual(status, 400)
                self.assertIn(key, payload["error"])
        self.assertEqual(self.call("/api/settings", [], content_type="application/json")[0], 400)
        self.assertEqual(self.call("/api/settings", {}, content_type="text/plain")[0], 415)
        self.assertEqual(self.call("/api/settings")[1]["settings"], settings.defaults())

    def test_resume_settings_apply_to_both_tools_on_pi_and_mac(self):
        socket = self.root / "codex.sock"
        daemon = FakeCodexDaemon(socket, [thread(SID2, "/work/codex", name="Codex session")])
        self.addCleanup(daemon.close)
        combined = make_app(self.store, self.app.index, self.sup, self.tmux, self.app.trash, socket,
                            registry=lambda: [], codex_trash=CodexTrash(self.root / "codex-trash.json"),
                            settings_file=self.app.settings_file)
        self.app.codex = combined.codex
        self.app.hosts = Hosts(self.root / "hosts")
        self.app.hosts.exchange("mac", {"version": 1, "rows": [
            {"id": sid, "tool": tool, "title": "Remote session", "cwd": "/Users/me/two words",
             "mtime": 100, "size": 42, "state": "closed", "mutable": True}
            for sid, tool in [(SID, "claude"), (SID2, "codex")]
        ]})
        status, _ = self.call("/api/settings", {"claude_command": "claudep", "codex_command": "codexp"})
        self.assertEqual(status, 200)
        rows = {row["id"]: row for row in self.call("/api/sessions")[1]["sessions"]}
        self.assertIn(f"claudep --resume {SID}", rows[SID]["resume_cmd"])
        self.assertEqual(rows[SID2]["resume_cmd"], f"cd -- /work/codex && codexp resume {SID2}")
        self.assertEqual(rows[f"mac:{SID}"]["resume_cmd"], f"cd -- '/Users/me/two words' && claudep --resume {SID}")
        self.assertEqual(rows[f"mac:{SID2}"]["resume_cmd"], f"cd -- '/Users/me/two words' && codexp resume {SID2}")
        self.call("/api/settings", {"claude_command": "another_claude"})
        rows = self.call("/api/sessions")[1]["sessions"]
        self.assertTrue(all("another_claude --resume" in row["resume_cmd"] for row in rows if row["tool"] == "claude"))
        local_rows = {row["id"]: row for row in combined.list_sessions()["sessions"]}
        self.assertIn("another_claude --resume", local_rows[SID]["resume_cmd"])

    def test_move_rejects_missing_folder(self):
        status, body = self.call(f"/api/sessions/{SID}/move", {"cwd": "/no/such/folder"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
