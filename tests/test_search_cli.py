import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_switchboard.cli import main
from llm_switchboard.search_indexer import run_index

from search_fixture import CLAUDE_ID, append_jsonl, claude_path, claude_records


class SearchCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
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
        self.assertIn("claudepd --resume", output)

    def test_search_session_shows_context(self):
        code, output = self.run_cli("search", "cobalt", "--session", f"claude:{CLAUDE_ID[:8]}")
        self.assertEqual(code, 0)
        self.assertIn("> reply:", output)
        self.assertIn("cobalt reply", output)
        self.assertIn("claudepd --resume", output)

    def test_json_output_is_one_result_per_line(self):
        code, output = self.run_cli("search", "--json", "amber")
        self.assertEqual(code, 0)
        [line] = output.splitlines()
        result = json.loads(line)
        self.assertEqual((result["tool"], result["id"], result["cwd"], result["count"]),
                         ("claude", CLAUDE_ID, "/work/alpha", 1))
        self.assertIn("amber prompt", result["excerpt"])
        self.assertIn("claudepd --resume", result["resume_cmd"])
        code, output = self.run_cli("search", "--json", "nowhere-word")
        self.assertEqual((code, output), (0, ""))

    def test_no_index_yet_exits_3_so_scripts_can_fall_back(self):
        code, output = self.run_cli("search", "--json", "amber", index_file=self.root / "absent.sqlite3")
        self.assertEqual((code, output), (3, ""))
        self.assertIn("isn't built yet", self.errors)


if __name__ == "__main__":
    unittest.main()
