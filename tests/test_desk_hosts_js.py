"""The tablet page's Mac link Node tests (tests/js/desk_*.test.js): targets
as (host, session_id), the link and refusal lines, candidates, Enroll now and
the host choice, through the pure modules and the real page."""

import subprocess
import unittest

from test_desk_conversation_js import NODE_SUITE_BOUND_SECONDS, ROOT, node

JS_TESTS = sorted((ROOT / "tests" / "js").glob("desk_*.test.js"))


class DeskHostsNodeTests(unittest.TestCase):
    def test_node_suites(self):
        self.assertTrue(JS_TESTS, "tests/js/desk_*.test.js is missing")
        proc = subprocess.run([node(), "--test", *map(str, JS_TESTS)], capture_output=True, text=True,
                              timeout=NODE_SUITE_BOUND_SECONDS, check=False, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
