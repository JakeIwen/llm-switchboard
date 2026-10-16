import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.trash import Trash
from llm_switchboard.web import App, make_server

from fakes import SID, SID2, Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript


class WebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "sessions.json")
        index = FakeIndex(transcript())
        self.tmux = FakeTmux()
        self.sup = Supervisor(self.store, index, self.tmux, FakeConnectivity(), registry=lambda: [], clock=Clock())
        server = make_server(App(self.store, index, self.sup, self.tmux, Trash(Path(tmp.name) / "trash"), registry=lambda: []), "127.0.0.1", 0)
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

    def test_serves_the_page_and_its_modules(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            self.assertIn(b"LLM Switchboard", resp.read())
        with urllib.request.urlopen(self.base + "/static/app.js") as resp:
            self.assertEqual(resp.headers["Content-Type"], "text/javascript; charset=utf-8")
        for path in ("/static/nope.js", "/static/../web.py", "/static/index.html", "/static/%2e%2e/web.py"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as ctx:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(ctx.exception.code, 404, path)

    def test_move_rejects_missing_folder(self):
        status, body = self.call(f"/api/sessions/{SID}/move", {"cwd": "/no/such/folder"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
