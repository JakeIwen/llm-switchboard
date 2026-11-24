"""The tablet page's Mac link Node tests (tests/js/desk_*.test.js): targets
as (host, session_id), the link and refusal lines, candidates, Enroll now and
the host choice, through the pure modules and the real page."""

import json
import subprocess
import unittest

from llm_switchboard.desk.remote_enrollment import ENROLL_NOW_NOTICE
from test_desk_conversation_js import NODE_SUITE_BOUND_SECONDS, ROOT, node

JS_TESTS = sorted((ROOT / "tests" / "js").glob("desk_*.test.js"))
PROBE = ROOT / "tests" / "js" / "enroll_now_probe.js"


class DeskHostsNodeTests(unittest.TestCase):
    def test_node_suites(self):
        self.assertTrue(JS_TESTS, "tests/js/desk_*.test.js is missing")
        proc = subprocess.run([node(), "--test", *map(str, JS_TESTS)], capture_output=True, text=True,
                              timeout=NODE_SUITE_BOUND_SECONDS, check=False, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-2000:])

    def test_enroll_now_wording_page_and_cli(self):
        """The page half (the CLI half is in test_desk_host_enrollment.py):
        the real page's candidate button reads Enroll now, and its
        confirmation is the notice the CLI prints, word for word."""
        proc = subprocess.run([node(), str(PROBE), str(PROBE.parent)], capture_output=True, text=True,
                              timeout=NODE_SUITE_BOUND_SECONDS, check=False, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(json.loads(proc.stdout),
                         {"button": "Enroll now", "notice": ENROLL_NOW_NOTICE, "confirm": "Enroll now"})


if __name__ == "__main__":
    unittest.main()
