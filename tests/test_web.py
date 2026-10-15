import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from claude_always_remote.store import Store
from claude_always_remote.supervisor import Supervisor
from claude_always_remote.web import App, make_server

from fakes import SID, SID2, Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript


class WebTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "sessions.json")
        index = FakeIndex(transcript())
        self.tmux = FakeTmux()
        self.sup = Supervisor(self.store, index, self.tmux, FakeConnectivity(), registry=lambda: [], clock=Clock())
        server = make_server(App(self.store, index, self.sup, self.tmux, registry=lambda: []), "127.0.0.1", 0)
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

    def test_serves_the_page(self):
        with urllib.request.urlopen(self.base + "/") as resp:
            self.assertIn(b"Always Remote", resp.read())


if __name__ == "__main__":
    unittest.main()
