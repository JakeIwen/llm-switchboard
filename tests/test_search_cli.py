import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_switchboard import settings
from llm_switchboard.cli import main
from llm_switchboard.search_indexer import run_index

from search_fixture import (CLAUDE_ID, CODEX_ID, append_jsonl, claude_path, claude_records,
                            codex_path, codex_records)


class SearchCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings_patch = patch("llm_switchboard.settings.SETTINGS_FILE", self.root / "settings.json")
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        append_jsonl(claude_path(self.root), claude_records())
        self.index_file = self.root / "search.sqlite3"
        run_index(self.index_file, self.root / "claude" / "projects", self.root / "codex", pause_s=0)

    def run_cli(self, *args, index_file=None):
        output, errors = io.StringIO(), io.StringIO()
        with patch("llm_switchboard.search_cli.SEARCH_INDEX_FILE", index_file or self.index_file), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(args))
        self.errors = errors.getvalue()
        return code, output.getvalue()

    def test_search_lists_count_excerpt_and_resume_command(self):
        code, output = self.run_cli("search", "amber")
        self.assertEqual(code, 0)
        self.assertIn("Alpha indexed session", output)
        self.assertIn("amber prompt", output)
        self.assertIn("claude --resume", output)

    def test_search_session_shows_context(self):
        code, output = self.run_cli("search", "cobalt", "--session", f"claude:{CLAUDE_ID[:8]}")
        self.assertEqual(code, 0)
        self.assertIn("> reply:", output)
        self.assertIn("cobalt reply", output)
        self.assertIn("claude --resume", output)

    def test_json_output_is_one_result_per_line(self):
        code, output = self.run_cli("search", "--json", "amber")
        self.assertEqual(code, 0)
        [line] = output.splitlines()
        result = json.loads(line)
        self.assertEqual((result["tool"], result["id"], result["cwd"], result["count"]),
                         ("claude", CLAUDE_ID, "/work/alpha", 1))
        self.assertIn("amber prompt", result["excerpt"])
        self.assertIn("claude --resume", result["resume_cmd"])
        code, output = self.run_cli("search", "--json", "nowhere-word")
        self.assertEqual((code, output), (0, ""))

    def add_entry(self, text):
        append_jsonl(claude_path(self.root), [
            {"type": "user", "cwd": "/work/alpha", "timestamp": "2026-10-20T10:00:00Z",
             "message": {"content": text}},
            {"type": "assistant", "timestamp": "2026-10-20T10:01:00Z",
             "message": {"content": [{"type": "text", "text": "neighboring entry"}]}},
        ])
        run_index(self.index_file, self.root / "claude" / "projects", self.root / "codex", pause_s=0)

    def test_context_before_after_and_explicit_zero(self):
        self.add_entry("first\r\nsecond\r\nNEEDLE here\r\nfourth\r\nfifth")
        for flags, expected in [
            (("-B", "1"), "second\nNEEDLE here"),
            (("--after", "1"), "NEEDLE here\nfourth"),
            (("--before", "1", "-A", "1"), "second\nNEEDLE here\nfourth"),
            (("--before", "0"), "NEEDLE here"),
            (("-B", "99", "-A", "99"), "first\nsecond\nNEEDLE here\nfourth\nfifth"),
        ]:
            with self.subTest(flags=flags):
                code, output = self.run_cli("search", "--json", *flags, "--", "needle")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["context"], expected)

    def test_context_merges_overlapping_and_adjacent_windows_and_separates_groups(self):
        lines = ["line0", "amber one", "line2", "COBALT two", "line4", "line5",
                 "amber three", "line7", "omitted8", "line9", "cobalt four", "line11"]
        self.add_entry("\n".join(lines))
        expected = "\n".join([*lines[:8], "--", *lines[9:]])
        code, output = self.run_cli("search", "--json", "-B", "1", "-A", "1", "--", "amber cobalt")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["context"], expected)
        code, output = self.run_cli("search", "-B", "1", "-A", "1", "--", "amber cobalt")
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[1:-1], [f"       {line}" for line in expected.split("\n")])

    def test_context_truncates_each_line_and_keeps_casefolded_match_visible(self):
        self.add_entry("\n".join(["x" * 400, "needle " + "y" * 400,
                                  "ß" * 350 + " NEEDLE " + "y" * 400, "z" * 400 + " needle"]))
        code, output = self.run_cli("search", "--json", "-B", "1", "--", "needle")
        self.assertEqual(code, 0)
        context = json.loads(output)["context"].split("\n")
        self.assertEqual(context[0], "x" * 300 + "…")
        self.assertEqual(context[1], "needle " + "y" * 293 + "…")
        self.assertEqual(context[2], "…" + "ß" * 99 + " NEEDLE " + "y" * 193 + "…")
        self.assertEqual(context[3], "…" + "z" * 293 + " needle")

    def test_context_falls_back_to_excerpt_when_fts_ignores_accents(self):
        self.add_entry("first line\ncafé here\nlast line")
        code, output = self.run_cli("search", "--json", "-B", "1", "-A", "1", "--", "cafe")
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result["context"], result["excerpt"])
        self.assertEqual(result["context"], "first line café here last line")

    def test_no_context_flags_keep_original_json_keys_and_excerpt(self):
        self.add_entry("first\nneedle here\nlast")
        code, output = self.run_cli("search", "--json", "needle")
        self.assertEqual(code, 0)
        original = json.loads(output)
        self.assertEqual(set(original), {"tool", "id", "short_id", "title", "cwd", "project", "mtime",
                                         "count", "matched_at", "excerpt", "resume_cmd", "host", "title_only"})
        self.assertEqual(original["excerpt"], "first needle here last")
        _, output = self.run_cli("search", "--json", "--after", "0", "--", "needle")
        with_context = json.loads(output)
        self.assertEqual(with_context.pop("context"), "needle here")
        self.assertEqual(with_context, original)
        _, output = self.run_cli("search", "needle")
        self.assertEqual(output.splitlines()[1], "       first needle here last")

    def test_context_preserves_codex_entry_newlines(self):
        append_jsonl(codex_path(self.root), [*codex_records(),
                     {"type": "event_msg", "timestamp": "2026-10-20T10:00:00Z",
                      "payload": {"type": "agent_message", "message": "before\nneedle reply\nafter"}}])
        run_index(self.index_file, self.root / "claude" / "projects", self.root / "codex", pause_s=0)
        code, output = self.run_cli("search", "--json", "-B", "1", "-A", "1", "--", "needle")
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result["id"], CODEX_ID)
        self.assertEqual(result["context"], "before\nneedle reply\nafter")

    def test_session_mode_ignores_context_flags(self):
        _, original = self.run_cli("search", "--json", "amber", "--session", f"claude:{CLAUDE_ID}")
        code, output = self.run_cli("search", "--json", "-B", "2", "-A", "2", "--session",
                                    f"claude:{CLAUDE_ID}", "--", "amber")
        self.assertEqual((code, output), (0, original))

    def test_context_flags_reject_negative_and_noninteger_counts(self):
        for flag in ("-B", "--before", "-A", "--after"):
            for value in ("-1", "1.5", "invalid"):
                with self.subTest(flag=flag, value=value), self.assertRaises(SystemExit) as raised:
                    self.run_cli("search", "--json", flag, value, "--", "amber")
                self.assertEqual(raised.exception.code, 2)

    def test_settings_changes_apply_without_reindexing_and_keep_json_shape(self):
        _, output = self.run_cli("search", "--json", "amber")
        before = json.loads(output)
        settings.save({"claude_command": "claudep"})
        code, output = self.run_cli("search", "--json", "amber")
        after = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(set(before), set(after))
        self.assertEqual(after["resume_cmd"], f"cd -- /work/alpha && claudep --resume {CLAUDE_ID}")
        code, output = self.run_cli("search", "--json", "amber", "--session", f"claude:{CLAUDE_ID}")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["result"]["resume_cmd"], after["resume_cmd"])

    def test_no_index_yet_exits_3_so_scripts_can_fall_back(self):
        code, output = self.run_cli("search", "--json", "amber", index_file=self.root / "absent.sqlite3")
        self.assertEqual((code, output), (3, ""))
        self.assertIn("isn't built yet", self.errors)

    def test_quiet_index_prints_only_when_a_transcript_changed(self):
        # The Mac's launchd job runs `index --quiet` every 30 s into a log file.
        with patch("llm_switchboard.search_cli.PROJECTS_DIR", self.root / "claude" / "projects"), \
                patch("llm_switchboard.search_cli.CODEX_DIR", self.root / "codex"):
            self.assertEqual(self.run_cli("index", "--quiet"), (0, ""))
            append_jsonl(claude_path(self.root), claude_records())
            code, output = self.run_cli("index", "--quiet")
        self.assertEqual(code, 0)
        self.assertIn("1 changed", output)

    def test_the_command_line_search_does_not_import_the_web_app(self):
        # llmsearch.sh and serve's 30 s indexer start this command each time.
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
            result = subprocess.run(
                [sys.executable, "-X", "importtime", "-m", "llm_switchboard", "search", "--json", "amber"],
                cwd=Path(__file__).resolve().parents[1], env={**env, "HOME": home},
                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 3, result.stderr[-500:])
        imported = {line.rsplit("|", 1)[-1].strip() for line in result.stderr.splitlines()
                    if line.startswith("import time:")}
        self.assertIn("llm_switchboard.search_cli", imported)
        self.assertFalse({"llm_switchboard.cli", "llm_switchboard.web"} & imported)


if __name__ == "__main__":
    unittest.main()
