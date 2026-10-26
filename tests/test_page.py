"""The root picker: resolver guards, cache headers and server delivery."""

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from llm_switchboard import page as picker_page
from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.trash import Trash
from llm_switchboard.web import App, make_server

from fakes import Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript


def build(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>picker</title>")
    (dist / "assets" / "index-Ab12.js").write_text("console.log(1)")
    (dist / "theme.js").write_text("/* first paint */")
    return dist


class ResolveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dist = build(Path(tmp.name))

    def resolve(self, path):
        return picker_page.resolve(path, self.dist)

    def test_api_paths_never_get_the_page_even_when_unbuilt(self):
        for dist in (self.dist, self.dist.parent / "nowhere"):
            for path in ("/api", "/api/", "/api/sessions", "/api/nope?x=1"):
                self.assertIsNone(picker_page.resolve(path, dist), path)

    def test_index_is_never_cached(self):
        for path in ("/", "/index.html", "/?q=1"):
            page = self.resolve(path)
            self.assertEqual((page.status, page.content_type, page.cache_control),
                             (200, "text/html; charset=utf-8", "no-store"), path)

    def test_hashed_assets_are_cached_for_good_and_the_rest_not(self):
        asset = self.resolve("/assets/index-Ab12.js?v=1")
        self.assertEqual((asset.status, asset.cache_control), (200, picker_page.IMMUTABLE))
        self.assertEqual(asset.content_type, "text/javascript; charset=utf-8")
        self.assertEqual(self.resolve("/theme.js").cache_control, "no-store")

    def test_preview_bookmarks_redirect_permanently_with_the_query(self):
        for source, destination in [("/next", "/"), ("/next/", "/"),
                                    ("/next?x=1&x=%2F", "/?x=1&x=%2F"),
                                    ("/next/index.html", "/index.html"),
                                    ("/next/settings?q=two%20words", "/settings?q=two%20words"),
                                    ("/next/assets/index-Ab12.js?v=1", "/assets/index-Ab12.js?v=1")]:
            for dist in (self.dist, self.dist.parent / "nowhere"):
                page = picker_page.resolve(source, dist)
                self.assertEqual((page.status, page.headers), (308, {"Location": destination}), source)

    def test_preview_redirect_cannot_point_at_another_origin(self):
        for path in ("/next//example.test", "/next/\\example.test"):
            self.assertEqual(self.resolve(path).status, 404, path)

    def test_paths_outside_dist_and_missing_files_are_not_found(self):
        for path in ("/../web.py", "/assets/../../x.js", "/assets/%2e%2e/x.js",
                     "/.hidden.js", "/assets/missing.js", "/a/b/c.js", "/x.exe",
                     "/assets%2findex-Ab12.js", "/%2e%2e", "/a/b"):
            self.assertEqual(self.resolve(path).status, 404, path)

    def test_a_client_route_gets_the_index(self):
        for path in ("/settings", "/settings?q=1", "/nextish"):
            self.assertIn(b"<title>picker</title>", self.resolve(path).body)

    def test_unbuilt_says_how_to_build(self):
        page = picker_page.resolve("/", self.dist.parent / "nowhere")
        self.assertEqual((page.status, page.content_type, page.cache_control),
                         (503, "text/plain; charset=utf-8", "no-store"))
        self.assertIn(b"npm ci && npm run build", page.body)
        self.assertIn(b"web/", page.body)


class ServeTest(unittest.TestCase):
    def test_server_sends_the_resolved_page_with_its_headers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        dist = build(root)
        store, index, tmux = Store(root / "sessions.json"), FakeIndex(transcript()), FakeTmux()
        sup = Supervisor(store, index, tmux, FakeConnectivity(), registry=lambda: [], clock=Clock())
        app = App(store, index, sup, tmux, Trash(root / "trash"), registry=lambda: [],
                  settings_file=root / "settings.json")
        server = make_server(app, "127.0.0.1", 0, strict_responses=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with mock.patch.object(picker_page, "DIST_DIR", dist):
            with urllib.request.urlopen(base + "/assets/index-Ab12.js") as resp:
                self.assertEqual(resp.headers["Cache-Control"], picker_page.IMMUTABLE)
                self.assertEqual(resp.read(), b"console.log(1)")
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(base + "/assets/missing.js")
            self.assertEqual(missing.exception.code, 404)
