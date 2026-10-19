import contextlib
import io
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

    def run_cli(self, *args):
        output = io.StringIO()
        with patch("llm_switchboard.cli.SEARCH_INDEX_FILE", self.index_file), contextlib.redirect_stdout(output):
            code = main(list(args))
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


if __name__ == "__main__":
    unittest.main()
