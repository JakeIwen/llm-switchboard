import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from llm_switchboard.search_index import TranscriptSearch
from llm_switchboard.search_indexer import run_index
from llm_switchboard.search_query import parse_query

from search_fixture import (CLAUDE_ID, CODEX_ID, append_jsonl, claude_path, claude_records,
                            codex_path, codex_records)

SCRIPTS = Path(os.environ.get("SESSION_SEARCH_SCRIPTS", Path.home() / "scripts"))
CLAUDE_SCRIPT = SCRIPTS / "claudesearch.sh"
CODEX_SCRIPT = SCRIPTS / "codexsearch.sh"
LLM_SCRIPT = SCRIPTS / "llmsearch.sh"
SCRIPTS_AVAILABLE = all(path.is_file() for path in (CLAUDE_SCRIPT, CODEX_SCRIPT, LLM_SCRIPT))


@unittest.skipUnless(SCRIPTS_AVAILABLE,
                     f"search oracle scripts are absent under {SCRIPTS}")
class SearchOracleTest(unittest.TestCase):
    """For words in prompts and replies, the index finds the sessions the
    scripts do. The scripts also search tool output and thinking, which the
    index leaves out (test_search_index covers that), so no query here uses them."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        append_jsonl(claude_path(self.root), claude_records())
        append_jsonl(codex_path(self.root), codex_records())
        append_jsonl(claude_path(self.root),
                     [{"type": "user", "message": {"content": "shared oracle phrase"}}])
        append_jsonl(codex_path(self.root),
                     [{"type": "event_msg", "payload": {"type": "agent_message",
                                                        "message": "shared oracle phrase"}}])
        append_jsonl(self.root / "codex" / "session_index.jsonl",
                     [{"id": CODEX_ID, "thread_name": "Oracle Codex"}])
        append_jsonl(claude_path(self.root).with_suffix("") / "subagents" / "agent.jsonl",
                     [{"type": "user", "message": {"content": "subagent-oracle-only"}}])
        append_jsonl(claude_path(self.root),
                     [{"type": "user", "isMeta": True,
                       "message": {"content": "injected-oracle-only"}}])
        self.index_file = self.root / "search.sqlite3"
        run_index(self.index_file, self.root / "claude" / "projects", self.root / "codex", pause_s=0)

    def oracle(self, provider, query):
        env = dict(os.environ, SESSION_SEARCH_RECORDS="1")
        if provider == "claude":
            env["CLAUDESEARCH_ROOT"] = str(self.root / "claude")
            script = CLAUDE_SCRIPT
        else:
            env["CODEXSEARCH_ROOT"] = str(self.root / "codex")
            script = CODEX_SCRIPT
        result = subprocess.run([str(script), "--", query], env=env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {(json.loads(line)["provider"], json.loads(line)["cwd"])
                for line in result.stdout.splitlines() if line.strip()}

    def combined_oracle(self, query):
        env = dict(os.environ, SESSION_SEARCH_RECORDS="1",
                   CLAUDESEARCH_ROOT=str(self.root / "claude"),
                   CODEXSEARCH_ROOT=str(self.root / "codex"))
        result = subprocess.run([str(LLM_SCRIPT), "--", query], env=env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {(json.loads(line)["provider"], json.loads(line)["cwd"])
                for line in result.stdout.splitlines() if line.strip()}

    def indexed(self, provider, query):
        rows = TranscriptSearch(self.index_file).search(parse_query(query))
        return {(row["tool"], row["cwd"]) for row in rows
                if provider is None or row["tool"] == provider}

    def test_combined_sessions_match_llmsearch_oracle(self):
        for product_query, oracle_query in (("amber", "amber"),
                                            ("codex prompt", "codex prompt"),
                                            ('"shared oracle phrase"', "shared oracle phrase")):
            with self.subTest(query=product_query):
                self.assertEqual(self.indexed(None, product_query), self.combined_oracle(oracle_query))

    def test_claude_sessions_match_script_for_literal_query_shapes(self):
        for product_query, oracle_query in (("amber", "amber"),
                                            ("several words", "several words"),
                                            ('"quoted phrase"', "quoted phrase")):
            with self.subTest(query=product_query):
                self.assertEqual(self.indexed("claude", product_query),
                                 self.oracle("claude", oracle_query))

    def test_codex_sessions_match_script_for_literal_query_shapes(self):
        for product_query, oracle_query in (("ochre", "ochre"),
                                            ("codex prompt", "codex prompt"),
                                            ('"teal codex"', "teal codex")):
            with self.subTest(query=product_query):
                self.assertEqual(self.indexed("codex", product_query),
                                 self.oracle("codex", oracle_query))

    def test_claude_exclusions_match_oracle(self):
        self.assertEqual(self.indexed("claude", "injected-oracle-only"),
                         self.oracle("claude", "injected-oracle-only"))
        self.assertEqual(self.indexed("claude", "subagent-oracle-only"),
                         self.oracle("claude", "subagent-oracle-only"))


if __name__ == "__main__":
    unittest.main()
