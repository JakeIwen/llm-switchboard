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

from search_fixture import CLAUDE_ID, append_jsonl, claude_path, claude_records


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
