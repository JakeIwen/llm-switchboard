"""HTTP encoding and validation contracts independent of the old-server oracle."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from llm_switchboard.codex_threads import CodexThread

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.trash import Trash
from llm_switchboard.web import App, create_http_app, openapi_schema
from fakes import Clock, FakeConnectivity, FakeIndex, FakeTmux, SID, transcript


class HttpBoundaryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store, tmux, index = Store(root / "sessions.json"), FakeTmux(), FakeIndex(transcript())
        supervisor = Supervisor(store, index, tmux, FakeConnectivity(), registry=lambda: [], clock=Clock())
        self.domain = App(store, index, supervisor, tmux, Trash(root / "trash"), registry=lambda: [],
                          settings_file=root / "settings.json")
        self.app = create_http_app(self.domain)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_bad_json_and_empty_bodies_keep_exact_legacy_errors(self):
        for raw, message in [(b"[1]", "body must be a JSON object"),
                             (b"null", "body must be a JSON object"),
                             (b"{", "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"),
                             (b"x" * 16385, "request body too large")]:
            with self.subTest(raw=raw[:20]):
                response = self.client.post("/api/settings", content=raw, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"error": message})
                self.assertEqual(response.headers["Content-Type"], "application/json")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = self.client.post("/api/settings", content=b"", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 200)

    def test_state_and_path_errors_precede_body_checks_as_before(self):
        cases = [("/no-such-route", 404, "not found"),
                 ("/api/hosts/UPPER/sessions/nope/rename", 404, "not found"),
                 ("/api/sessions/nope/rename", 400, "bad session id"),
                 ("/api/sessions/part%2Fpart/rename", 400, "bad session id"),
                 (f"/api/sessions/{SID}/ren%61me", 404, "not found"),
                 (f"/api/sessions/{SID}/rename", 415, "send application/json")]
        for path, status, error in cases:
            response = self.client.post(path, content="{")
            self.assertEqual((response.status_code, response.json()), (status, {"error": error}))

    def test_json_serialization_keeps_missing_keys_and_stdlib_bytes(self):
        for path, expected in [(f"/api/sessions/{SID}/screen", {"source": "none", "screen": ""}),
                               (f"/api/sessions/{SID}/name", {"ok": True, "enabled": False, "rc_name": "café"})]:
            response = (self.client.post(path, json={"name": "café"}) if path.endswith("/name")
                        else self.client.get(path))
            self.assertEqual(response.content, json.dumps(expected).encode())

    def test_new_codex_states_pass_through_without_breaking_claude_rows(self):
        thread = CodexThread(SID, "/work", "Codex", "hello", 12, None, "futureState")
        self.domain.codex = SimpleNamespace(catalog=SimpleNamespace(threads=lambda: [thread], error=""))
        response = self.client.get("/api/sessions")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["sessions"]
        self.assertEqual({row["tool"] for row in rows}, {"claude", "codex"})
        self.assertEqual(next(row for row in rows if row["tool"] == "codex")["codex_state"], "futureState")

    def test_settings_uses_the_stdlib_validator_and_never_detail_or_422(self):
        cases = [({"folders": {"unknown": 3}}, "unknown settings key: folders.unknown"),
                 ({"folders": []}, "folders must be an object"),
                 ({"poll_seconds": True}, "poll_seconds must be an integer from 2 to 60")]
        for body, error in cases:
            response = self.client.post("/api/settings", json=body)
            self.assertEqual((response.status_code, response.json()), (400, {"error": error}))

    def test_all_documented_json_routes_have_response_models_and_no_422(self):
        for route in self.app.routes:
            if isinstance(route, APIRoute) and route.path.startswith("/api/"):
                self.assertIsNotNone(route.response_model, route.path)
        schema = openapi_schema()
        for item in schema["paths"].values():
            for operation in item.values():
                self.assertNotIn("422", operation["responses"])
        self.assertIn("SessionStatus", schema["components"]["schemas"])
        self.assertIn("SettingsBody", schema["components"]["schemas"])
        for path in ("/api/search", "/api/search/claude/{sid}", "/api/search/codex/{sid}"):
            parameters = {item["name"]: item for item in schema["paths"][path]["get"]["parameters"]}
            self.assertEqual(parameters["q"]["in"], "query")
            if "{sid}" in path:
                self.assertEqual(parameters["sid"]["in"], "path")
                self.assertTrue(parameters["sid"]["required"])

    def test_openapi_cli_is_deterministic_without_runtime_side_effects(self):
        command = [sys.executable, "-m", "llm_switchboard", "openapi"]
        outputs = [subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                  check=True, timeout=10).stdout for _ in range(2)]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(json.loads(outputs[0]), openapi_schema())
