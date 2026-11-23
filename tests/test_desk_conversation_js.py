"""Conversation mode on the page (static/conversation_logic.js and
static/conversation.js): runs the Node tests in tests/js/ and checks that the
values the page mirrors from the hub have not drifted."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from llm_switchboard.desk import grants
from llm_switchboard.desk.model import MAX_VOICE_TEXT_BYTES, ConfirmMode, RegistrationState, RequestState

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "llm_switchboard" / "desk" / "static"
JS_TESTS = sorted((ROOT / "tests" / "js").glob("conversation*.test.js"))
EXPORTS = """
const L = require(process.argv[1]);
const D = require(process.argv[2]);
process.stdout.write(JSON.stringify({
  confirm: L.CONFIRM, request: L.REQUEST_STATE, registration: L.REGISTRATION, limits: L.LIMITS,
  tooLong: L.draftProblem("a".repeat(L.LIMITS.MAX_DRAFT_BYTES + 1)), refusals: D.REFUSALS,
}));
"""


def node() -> str:
    found = shutil.which("node")
    if found is None:
        raise AssertionError("node is required by the suite")
    return found


class ConversationJsTests(unittest.TestCase):
    def test_node_suites(self):
        self.assertTrue(JS_TESTS, "tests/js/conversation*.test.js is missing")
        proc = subprocess.run([node(), "--test", *map(str, JS_TESTS)], capture_output=True, text=True,
                              timeout=120, check=False, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout[-6000:] + proc.stderr[-2000:])


class ConversationMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([node(), "-e", EXPORTS, str(STATIC / "conversation_logic.js"),
                               str(STATIC / "desk_logic.js")],
                              capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.js = json.loads(proc.stdout)

    def test_vocabularies_match_the_hub(self):
        self.assertEqual(sorted(self.js["confirm"].values()), sorted(m.value for m in ConfirmMode))
        self.assertEqual(sorted(self.js["request"].values()), sorted(s.value for s in RequestState))
        self.assertEqual(sorted(self.js["registration"].values()), sorted(s.value for s in RegistrationState))

    def test_the_voice_limit_and_its_refusal_match_the_hub(self):
        self.assertEqual(self.js["limits"]["MAX_DRAFT_BYTES"], MAX_VOICE_TEXT_BYTES)
        self.assertEqual(self.js["tooLong"], grants.VOICE_TEXT_TOO_LONG)

    def test_the_page_gives_up_on_a_grant_before_the_hub_drops_it(self):
        limits = self.js["limits"]
        self.assertLess(limits["FRESH_MS"], grants.GRANT_TTL_SECONDS * 1000)
        self.assertLess(limits["BEAT_ABORT_MS"], limits["BEAT_MS"])
        self.assertLess(limits["BEAT_MS"], limits["FRESH_MS"])

    def test_voice_send_refusals_are_definitive_on_the_page(self):
        # A send refused for these reasons was never handed off, so the page
        # may say "not sent"; any reason missing here reads as "unknown".
        for reason in (grants.VOICE_OFF, grants.GRANT_NOT_CURRENT, grants.GRANT_WRONG_TARGET,
                       grants.VOICE_TEXT_TOO_LONG, "bad confirm"):
            self.assertIn(reason, self.js["refusals"])


if __name__ == "__main__":
    unittest.main()
