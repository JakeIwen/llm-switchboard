import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from llm_switchboard.mac_search_server import MacSearchServer
from llm_switchboard.search_indexer import run_index

from search_fixture import append_jsonl, claude_path, claude_records, codex_path, codex_records

ORIGIN = "https://pi.example-tailnet.ts.net"


class MacSearchServerTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        append_jsonl(claude_path(root), claude_records())
        append_jsonl(codex_path(root), codex_records())
        self.index_file = root / "search.sqlite3"
        run_index(self.index_file, root / "claude" / "projects", root / "codex", pause_s=0)
        self.server = MacSearchServer("mac", port=0, index_file=self.index_file, origins=[ORIGIN])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def request(self, path="/health", method="GET", headers=None, server=None):
        connection = http.client.HTTPConnection(*(server or self.server).server_address, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), json.loads(body) if body else None
        finally:
            connection.close()

    def test_results_detail_status_and_health(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.request()[2], {"ok": True})
        for tool, query in [("claude", "amber"), ("codex", "ochre")]:
            with self.subTest(tool=tool):
                status, headers, payload = self.request(f"/api/search?q={query}")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                [result] = payload["results"]
                self.assertEqual((result["host"], result["tool"], result["title_only"]), ("mac", tool, False))
                status, _, detail = self.request(f"/api/search/{tool}/{result['id']}?q={query}")
                self.assertEqual(status, 200)
                self.assertEqual(detail["detail"]["result"]["host"], "mac")
                self.assertTrue(detail["detail"]["contexts"])
        self.assertEqual(self.request("/api/search/status")[2]["index"]["sessions"], 2)
        self.assertEqual(self.request("/api/search?q=")[0], 400)
        self.assertEqual(self.request("/missing")[0], 404)

    def test_host_guard_accepts_only_explicit_loopback_names_and_current_port(self):
        port = self.server.server_port
        for host in ["evil.example", "localhost", f"127.0.0.1:{port + 1}", f"evil.example:{port}"]:
            with self.subTest(host=host):
                self.assertEqual(self.request(headers={"Host": host})[0], 403)
        self.assertEqual(self.request(headers={"Host": f"localhost:{port}"})[0], 200)
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.putrequest("GET", "/health", skip_host=True)
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()

    def test_cors_allowlist(self):
        status, headers, _ = self.request("/api/search?q=amber", headers={"Origin": ORIGIN})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(headers["Vary"], "Origin")
        for origin in ["https://evil.example", "null", ORIGIN + ".evil.example"]:
            with self.subTest(origin=origin):
                _, headers, _ = self.request("/api/search?q=amber", headers={"Origin": origin})
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertNotIn("Access-Control-Allow-Private-Network", headers)

    def test_private_network_preflight_is_only_for_allowed_origins(self):
        for origin, expected in [(ORIGIN, 204), ("https://evil.example", 403), ("null", 403)]:
            with self.subTest(origin=origin):
                status, headers, _ = self.request("/api/search?q=amber", "OPTIONS", {
                    "Origin": origin, "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Private-Network": "true"})
                self.assertEqual(status, expected)
                if expected == 204:
                    self.assertEqual(headers["Access-Control-Allow-Origin"], origin)
                    self.assertEqual(headers["Access-Control-Allow-Methods"], "GET")
                    self.assertEqual(headers["Access-Control-Allow-Private-Network"], "true")
                    self.assertGreater(int(headers["Access-Control-Max-Age"]), 0)
                else:
                    self.assertNotIn("Access-Control-Allow-Origin", headers)
                    self.assertNotIn("Access-Control-Allow-Private-Network", headers)

    def test_non_read_methods_refused_without_reading_a_body(self):
        for method in ["POST", "PUT", "PATCH", "DELETE", "HEAD", "TRACE", "CUSTOM"]:
            with self.subTest(method=method):
                # A nonempty declared body that is never sent must not make the handler wait.
                status, headers, _ = self.request("/api/search?q=amber", method, {"Content-Length": "999"})
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, OPTIONS")

    def test_queries_never_appear_in_access_or_error_logs(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.request("/api/search?q=private-query-text")
            self.request("/missing?q=private-query-text")
            self.request("/api/search?q=private-query-text", "POST")
        self.assertNotIn("private-query-text", out.getvalue())

    def test_empty_origins_log_once_and_disable_cors(self):
        with self.assertLogs("llm_switchboard.mac_search_server", level="WARNING") as logs:
            server = MacSearchServer("mac", port=0, index_file=self.index_file)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.origins, frozenset())
        self.assertEqual(len(logs.output), 1)
        status, headers, _ = self.request(headers={"Origin": ORIGIN}, server=server)
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, _ = self.request(method="OPTIONS", headers={"Origin": ORIGIN}, server=server)
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Private-Network", headers)

    def test_busy_port_and_invalid_host_name_fail(self):
        with self.assertRaises(OSError):
            MacSearchServer("mac", port=self.server.server_port, index_file=self.index_file)
        with self.assertRaises(ValueError):
            MacSearchServer("../bad", port=0, index_file=self.index_file)


if __name__ == "__main__":
    unittest.main()
