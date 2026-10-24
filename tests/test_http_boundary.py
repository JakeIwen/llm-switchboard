"""HTTP encoding and validation contracts independent of the old-server oracle."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from llm_switchboard.hosts import Hosts

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
        self.app = create_http_app(self.domain, strict_responses=True)
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

    def test_production_schema_drift_logs_route_but_preserves_original_payload(self):
        payload = self.domain.list_sessions()
        payload["sessions"][0]["future_field"] = "private fixture value"
        with patch.dict(os.environ, {"LLM_SWITCHBOARD_STRICT_RESPONSES": ""}):
            app = create_http_app(self.domain)
        with TestClient(app) as client, patch.object(self.domain, "list_sessions", return_value=payload):
            with self.assertLogs("llm_switchboard.http.transport", level="ERROR") as logs:
                response = client.get("/api/sessions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, json.dumps(payload).encode())
        self.assertEqual(response.headers["Content-Type"], "application/json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("GET /api/sessions", logs.output[0])
        self.assertIn("future_field", logs.output[0])
        self.assertNotIn("private fixture value", logs.output[0])

    def test_strict_response_flag_is_fatal_and_explicit_parameter_overrides_it(self):
        payload = {**self.domain.settings_payload(), "future_field": True}
        with patch.dict(os.environ, {"LLM_SWITCHBOARD_STRICT_RESPONSES": "1"}):
            strict = create_http_app(self.domain)
            permissive = create_http_app(self.domain, strict_responses=False)
        with patch.object(self.domain, "settings_payload", return_value=payload):
            with TestClient(strict) as client:
                with self.assertLogs("llm_switchboard.http.transport", level="ERROR"):
                    with self.assertRaises(ValidationError):
                        client.get("/api/settings")
            with TestClient(permissive) as client:
                with self.assertLogs("llm_switchboard.http.transport", level="ERROR"):
                    self.assertEqual(client.get("/api/settings").json(), payload)

    def test_unexpected_collection_lookup_errors_are_logged_500_not_404(self):
        cases = [("GET", "/api/sessions", "list_sessions"),
                 ("GET", "/api/folders", "list_folders"),
                 ("GET", "/api/trash", "list_trash"),
                 ("GET", "/api/settings", "settings_payload"),
                 ("POST", "/api/sessions/new", "create")]
        with TestClient(self.app, raise_server_exceptions=False) as client:
            for method, path, function in cases:
                with self.subTest(path=path), patch.object(self.domain, function, side_effect=KeyError("broken record")):
                    with self.assertLogs("llm_switchboard.http.transport", level="ERROR") as logs:
                        response = client.request(method, path, json={} if method == "POST" else None)
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.json(), {"error": "internal server error"})
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertIn(path, logs.output[0])
                    self.assertIn("Traceback", logs.output[0])
                    self.assertIsNotNone(logs.records[0].exc_info)
        with patch.object(self.domain, "messages", side_effect=LookupError("missing transcript")):
            response = self.client.get(f"/api/sessions/{SID}/messages")
            self.assertEqual((response.status_code, response.json()), (404, {"error": "missing transcript"}))

    def test_successful_posts_restore_legacy_audit_wording(self):
        self.domain.hosts = Hosts(self.domain.settings_file.parent / "hosts")
        self.domain.hosts.exchange("mac", {"version": 1, "rows": [
            {"id": SID, "tool": "claude", "title": "Remote", "cwd": "/Users/me/work",
             "mtime": 100, "size": 42, "state": "closed", "mutable": True}]})
        cases = [("/api/settings", {}, "settings"),
                 ("/api/trash/empty", {}, "empty trash"),
                 (f"/api/sessions/{SID}/name", {"name": "Label"}, f"name {SID[:8]}"),
                 ("/api/sessions/new", {"name": "New"}, "new"),
                 (f"/api/hosts/mac/sessions/{SID}/rename", {"title": "Renamed"}, "mac rename")]
        with patch.object(self.domain, "create", return_value={"ok": True, "id": SID, "title": "New"}):
            for path, body, expected in cases:
                with self.subTest(path=path), self.assertLogs("llm_switchboard.http.transport", level="INFO") as logs:
                    self.assertEqual(self.client.post(path, json=body).status_code, 200)
                self.assertEqual([record.getMessage() for record in logs.records], [f"{expected} from testclient"])
        with self.assertNoLogs("llm_switchboard.http.transport", level="INFO"):
            self.assertEqual(self.client.get("/api/settings").status_code, 200)
            self.assertEqual(self.client.post("/api/settings", json={"poll_seconds": -1}).status_code, 400)

    def test_openapi_cli_is_deterministic_without_runtime_side_effects(self):
        command = [sys.executable, "-m", "llm_switchboard", "openapi"]
        outputs = [subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                  check=True, timeout=10).stdout for _ in range(2)]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(json.loads(outputs[0]), openapi_schema())
