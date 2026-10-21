"""Runs the page's required JavaScript unit tests (tests/js) under Node."""

import shutil
import subprocess
import unittest
from pathlib import Path

JS_TESTS = Path(__file__).parent / "js"


class JsTest(unittest.TestCase):
    def test_js_suite(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required to run the documented JavaScript test suite")
        files = sorted(str(p) for p in JS_TESTS.glob("*.test.mjs"))
        result = subprocess.run([node, "--test", *files], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
