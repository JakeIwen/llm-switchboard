"""The ASGI preview adapter sends next_page's byte/header contract unchanged."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from llm_switchboard import next_page
from llm_switchboard.web import create_http_app
from test_next_page import build


class PreviewTransportTest(unittest.TestCase):
    def test_resolver_results_are_identical_over_http_built_and_unbuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            built = build(root)
            with TestClient(create_http_app(strict_responses=True)) as client:
                for dist in (built, root / "unbuilt"):
                    with patch.object(next_page, "DIST_DIR", dist):
                        for path in ("/next", "/next/", "/next/?q=1", "/next/index.html", "/next/settings",
                                     "/next/assets/index-Ab12.js", "/next/assets/index-Ab12.js?v=1",
                                     "/next/theme.js", "/next/assets/%2e%2e/nope.js", "/next/missing.js"):
                            with self.subTest(dist=dist.name, path=path):
                                expected = next_page.resolve(path)
                                actual = client.get(path, follow_redirects=False)
                                self.assertEqual(actual.status_code, expected.status)
                                self.assertEqual(actual.content, expected.body)
                                self.assertEqual(actual.headers["Content-Type"], expected.content_type)
                                self.assertEqual(actual.headers["Cache-Control"], expected.cache_control)
                                for key, value in expected.headers.items():
                                    self.assertEqual(actual.headers[key], value)
