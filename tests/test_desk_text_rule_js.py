"""The page's text rule (static/text_rule.js) must agree with text_rule.py on the shared cases."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from llm_switchboard.desk.text_rule import check_text, normalize_text

DESK = Path(__file__).resolve().parents[1] / "llm_switchboard" / "desk"
CASES = DESK / "text_rule_cases.json"
SCRIPT = """
const rule = require(process.argv[1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = cases.check.map(t => rule.checkText(t));
const norm = cases.normalize.map(t => rule.normalizeText(t));
process.stdout.write(JSON.stringify({check: out, normalize: norm}));
"""


def expand(case: dict) -> str:
    return case["repeat"] * case["count"] if "repeat" in case else case["text"]


class DeskTextRuleJsTests(unittest.TestCase):
    def run_node(self, checks: list[str], normals: list[str]) -> dict:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required by the suite")
        proc = subprocess.run(
            [node, "-e", SCRIPT, str(DESK / "static" / "text_rule.js")],
            input=json.dumps({"check": checks, "normalize": normals}),
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_shared_cases(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))
        checks = [expand(c) for c in cases["check"]]
        got = self.run_node(checks, [c["text"] for c in cases["normalize"]])
        for case, text, reason in zip(cases["check"], checks, got["check"]):
            self.assertEqual(reason, case["reason"], repr(text[:40]))
        for case, normalized in zip(cases["normalize"], got["normalize"]):
            self.assertEqual(normalized, case["normalized"])

    def test_agrees_with_python_on_edge_characters(self):
        # Characters where Python's strip() and JS trim() differ, plus
        # separators, bidi marks and astral characters.
        samples = ["\x1cx", "x\x1f", "\x85x", "x﻿", "﻿x", " x", "x　",
                   "a b", "a​b", "a‮b", "😀", "x\x0by", "\x0cx", "a\u0080b"]
        got = self.run_node(samples, samples)
        for text, reason, normalized in zip(samples, got["check"], got["normalize"]):
            self.assertEqual(reason, check_text(text), repr(text))
            self.assertEqual(normalized, normalize_text(text), repr(text))

    def test_lone_surrogates_are_invalid(self):
        # JSON carries them as \\ud800 escapes; Python can't hold them via
        # json.dumps ensure_ascii=False, so ensure_ascii keeps the escape.
        samples = ["a\ud800b", "\udc00", "ok\ud83d"]
        got = self.run_node(samples, [])
        for text, reason in zip(samples, got["check"]):
            self.assertEqual(reason, check_text(text), repr(text))
            self.assertEqual(reason, "invalid character")


if __name__ == "__main__":
    unittest.main()
