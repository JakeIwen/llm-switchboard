"""Runs the page's JavaScript unit tests (tests/js) under node, when node is installed."""

import shutil
import subprocess
import unittest
from pathlib import Path

JS_TESTS = Path(__file__).parent / "js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JsTest(unittest.TestCase):
    def test_js_suite(self):
        files = sorted(str(p) for p in JS_TESTS.glob("*.test.mjs"))
        result = subprocess.run(["node", "--test", *files], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
