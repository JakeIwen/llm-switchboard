import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from llm_switchboard import settings
from llm_switchboard.search_api import SearchHandlers
from llm_switchboard.search_indexer import run_index
from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.trash import Trash
from llm_switchboard.web import App, make_server

from fakes import Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript
from search_fixture import append_jsonl, claude_path, claude_records, codex_path, codex_records


class SearchWebTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.settings_file = root / "settings.json"
        append_jsonl(claude_path(root), claude_records())
        append_jsonl(codex_path(root), codex_records())
        index_file = root / "search.sqlite3"
        run_index(index_file, root / "claude" / "projects", root / "codex", pause_s=0)
        store = Store(root / "sessions.json")
        index = FakeIndex(transcript())
        tmux = FakeTmux()
        supervisor = Supervisor(store, index, tmux, FakeConnectivity(), registry=lambda: [], clock=Clock())
        app = App(store, index, supervisor, tmux, Trash(root / "trash"), registry=lambda: [],
                  search=SearchHandlers(index_file, lambda: settings.load(self.settings_file)),
                  settings_file=self.settings_file)
        server = make_server(app, "127.0.0.1", 0, strict_responses=True)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_result_status_and_context_routes(self):
        status, payload = self.get("/api/search?q=amber")
        self.assertEqual(status, 200)
        [result] = payload["results"]
        self.assertEqual(result["title"], "Alpha indexed session")
        self.assertEqual(payload["index"]["state"], "ready")
        status, detail = self.get(f"/api/search/claude/{result['id']}?q=amber")
        self.assertEqual(status, 200)
        self.assertTrue(detail["detail"]["contexts"])
        self.assertEqual(self.get("/api/search/status")[1]["index"]["sessions"], 2)

    def test_commands_follow_settings_after_indexing_for_results_and_context(self):
        for tool, query, command in [("claude", "amber", "claudep"), ("codex", "ochre", "codexp")]:
            with self.subTest(tool=tool):
                before = self.get(f"/api/search?q={query}")[1]["results"][0]
                settings.save({f"{tool}_command": command}, self.settings_file)
                after = self.get(f"/api/search?q={query}")[1]["results"][0]
                self.assertNotEqual(before["resume_cmd"], after["resume_cmd"])
                self.assertIn(command, after["resume_cmd"])
                status, payload = self.get(f"/api/search/{tool}/{after['id']}?q={query}")
                self.assertEqual(status, 200)
                self.assertEqual(payload["detail"]["result"]["resume_cmd"], after["resume_cmd"])

    def test_bad_query_is_a_400_and_fts_syntax_is_literal(self):
        self.assertEqual(self.get("/api/search?q=")[0], 400)
        query = urllib.parse.quote('"amber OR cobalt"')
        status, payload = self.get(f"/api/search?q={query}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["results"], [])


if __name__ == "__main__":
    unittest.main()
