"""The rebuilt page at /next/: what the resolver sends, and that the server sends it."""

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from llm_switchboard import next_page
from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.trash import Trash
from llm_switchboard.web import App, make_server

from fakes import Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript


def build(root: Path) -> Path:
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>next</title>")
    (dist / "assets" / "index-Ab12.js").write_text("console.log(1)")
    (dist / "theme.js").write_text("/* first paint */")
    return dist


class ResolveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dist = build(Path(tmp.name))

    def resolve(self, path):
        return next_page.resolve(path, self.dist)

    def test_other_paths_are_not_the_new_pages(self):
        for path in ("/", "/static/app.js", "/api/sessions", "/nextish"):
            self.assertIsNone(self.resolve(path), path)

    def test_index_is_never_cached(self):
        for path in ("/next/", "/next/index.html", "/next/?q=1"):
            page = self.resolve(path)
            self.assertEqual((page.status, page.content_type, page.cache_control),
                             (200, "text/html; charset=utf-8", "no-store"), path)

    def test_hashed_assets_are_cached_for_good_and_the_rest_not(self):
        asset = self.resolve("/next/assets/index-Ab12.js")
        self.assertEqual((asset.status, asset.cache_control), (200, next_page.IMMUTABLE))
        self.assertEqual(asset.content_type, "text/javascript; charset=utf-8")
        self.assertEqual(self.resolve("/next/theme.js").cache_control, "no-store")

    def test_without_trailing_slash_redirects(self):
        page = self.resolve("/next")
        self.assertEqual((page.status, page.headers), (308, {"Location": "/next/"}))

    def test_paths_outside_dist_and_missing_files_are_not_found(self):
        for path in ("/next/../web.py", "/next/assets/../../x.js", "/next/assets/%2e%2e/x.js",
                     "/next/.hidden.js", "/next/assets/missing.js", "/next/a/b/c.js", "/next/x.exe"):
            self.assertEqual(self.resolve(path).status, 404, path)

    def test_a_client_route_gets_the_index(self):
        self.assertIn(b"<title>next</title>", self.resolve("/next/settings").body)

    def test_unbuilt_says_how_to_build(self):
        page = next_page.resolve("/next/", self.dist.parent / "nowhere")
        self.assertEqual(page.status, 503)
        self.assertIn(b"npm run build", page.body)


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
        server = make_server(app, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with mock.patch.object(next_page, "DIST_DIR", dist):
            with urllib.request.urlopen(base + "/next/assets/index-Ab12.js") as resp:
                self.assertEqual(resp.headers["Cache-Control"], next_page.IMMUTABLE)
                self.assertEqual(resp.read(), b"console.log(1)")
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(base + "/next/assets/missing.js")
            self.assertEqual(missing.exception.code, 404)
