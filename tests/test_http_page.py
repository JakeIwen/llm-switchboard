"""The ASGI page adapter sends the resolver's byte/header contract unchanged."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from llm_switchboard import page as picker_page
from llm_switchboard.web import create_http_app
from test_page import build


class PageTransportTest(unittest.TestCase):
    def test_resolver_results_are_identical_over_http_built_and_unbuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            built = build(root)
            with TestClient(create_http_app(strict_responses=True)) as client:
                for dist in (built, root / "unbuilt"):
                    with patch.object(picker_page, "DIST_DIR", dist):
                        for path in ("/", "/?q=1", "/index.html", "/settings",
                                     "/assets/index-Ab12.js", "/assets/index-Ab12.js?v=1",
                                     "/theme.js", "/assets/%2e%2e/nope.js", "/missing.js",
                                     "/next", "/next/?q=1", "/next/settings?q=%2F&x=2",
                                     "/next/assets/index-Ab12.js?v=1", "/next//example.test"):
                            with self.subTest(dist=dist.name, path=path):
                                expected = picker_page.resolve(path)
                                actual = client.get(path, follow_redirects=False)
                                self.assertEqual(actual.status_code, expected.status)
                                self.assertEqual(actual.content, expected.body)
                                self.assertEqual(actual.headers["Content-Type"], expected.content_type)
                                self.assertEqual(actual.headers["Cache-Control"], expected.cache_control)
                                for key, value in expected.headers.items():
                                    self.assertEqual(actual.headers[key], value)

    def test_api_misses_remain_exact_json_404s_even_without_a_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = build(Path(tmp))
            with TestClient(create_http_app(strict_responses=True)) as client:
                for dist in (built, Path(tmp) / "unbuilt"):
                    with patch.object(picker_page, "DIST_DIR", dist):
                        for path in ("/api", "/api/", "/api/missing", "/api/missing?q=1",
                                     "/api/settings?x=1", "/api/%73ettings", "/api/sessions/"):
                            with self.subTest(dist=dist.name, path=path):
                                actual = client.get(path)
                                self.assertEqual(actual.status_code, 404)
                                self.assertEqual(actual.content, b'{"error": "not found"}')
                                self.assertEqual(actual.headers["Content-Type"], "application/json")
                                self.assertEqual(actual.headers["Cache-Control"], "no-store")
