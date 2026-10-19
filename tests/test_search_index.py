import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from llm_switchboard.search_index import TranscriptSearch
from llm_switchboard.search_indexer import run_index
from llm_switchboard.search_query import parse_query

from search_fixture import (CLAUDE_ID, CODEX_ID, append_jsonl, claude_path, claude_records,
                            codex_path, codex_records)


class SearchIndexTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.projects = self.root / "claude" / "projects"
        self.codex = self.root / "codex"
        self.index_file = self.root / "data" / "search.sqlite3"
        append_jsonl(claude_path(self.root), claude_records())
        append_jsonl(codex_path(self.root), codex_records())
        append_jsonl(self.codex / "session_index.jsonl",
                     [{"id": CODEX_ID, "thread_name": "Named Codex thread"}])
        self.run_index()

    def run_index(self):
        return run_index(self.index_file, self.projects, self.codex, pause_s=0)

    def search(self, query):
        return TranscriptSearch(self.index_file).search(parse_query(query, now=time.time()))

    def test_indexes_claude_and_codex_with_titles_and_resume_commands(self):
        [claude] = self.search("amber")
        self.assertEqual((claude["tool"], claude["id"], claude["title"]),
                         ("claude", CLAUDE_ID, "Alpha indexed session"))
        self.assertIn("claudepd --resume", claude["resume_cmd"])
        [codex] = self.search("ochre")
        self.assertEqual((codex["tool"], codex["title"]), ("codex", "Named Codex thread"))
        self.assertIn("codexp resume", codex["resume_cmd"])
        status = TranscriptSearch(self.index_file).status()
        self.assertEqual((status["state"], status["sessions"], status["files"]), ("ready", 2, 2))

    def test_incremental_append_indexes_only_complete_lines(self):
        path = claude_path(self.root)
        partial = {"type": "assistant", "timestamp": "2026-10-10T11:00:00Z",
                   "message": {"content": [{"type": "text", "text": "incremental silver"}]}}
        with path.open("a") as out:
            out.write(json.dumps(partial))
        first = self.run_index()
        self.assertEqual(self.search("silver"), [])
        with path.open("a") as out:
            out.write("\n")
        second = self.run_index()
        self.assertEqual(len(self.search("silver")), 1)
        self.assertEqual(first.bytes_read, 0)
        self.assertEqual(first.changed, 1)
        self.assertGreater(second.bytes_read, 0)
        unchanged = self.run_index()
        self.assertEqual((unchanged.changed, unchanged.bytes_read), (0, 0))

    def test_shrink_rebuilds_and_vanished_file_is_dropped(self):
        path = claude_path(self.root)
        path.write_text(json.dumps({"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work/alpha",
                                    "message": {"content": "replacement magenta"}}) + "\n")
        self.run_index()
        self.assertEqual(self.search("amber"), [])
        self.assertEqual(len(self.search("magenta")), 1)
        path.unlink()
        stats = self.run_index()
        self.assertEqual(stats.removed, 1)
        self.assertEqual(self.search("magenta"), [])

    def test_who_tool_folder_and_age_filters(self):
        self.assertEqual(len(self.search("by:me amber")), 1)
        self.assertEqual(self.search("by:reply amber"), [])
        self.assertEqual(len(self.search("by:reply violet")), 1)
        self.assertEqual(len(self.search("by:tool scarlet")), 1)
        self.assertEqual(len(self.search("by:me gold")), 1)
        self.assertEqual(len(self.search("tool:codex ochre")), 1)
        self.assertEqual(len(self.search("by:me ochre")), 1)
        self.assertEqual(len(self.search("by:reply teal")), 1)
        self.assertEqual(len(self.search("by:tool umber")), 1)
        self.assertEqual(self.search("tool:claude ochre"), [])
        self.assertEqual(len(self.search("dir:alpha amber")), 1)
        self.assertEqual(self.search("dir:elsewhere amber"), [])
        old = time.time() - 10 * 86400
        os.utime(claude_path(self.root), (old, old))
        self.run_index()
        self.assertEqual(self.search("age:<2d amber"), [])
        self.assertEqual(len(self.search("age:>1w amber")), 1)

    def test_all_terms_share_a_matching_event_and_context_surrounds_hits(self):
        [result] = self.search("amber several")
        self.assertEqual(result["count"], 1)
        self.assertEqual(self.search("amber cobalt"), [])
        detail = TranscriptSearch(self.index_file).detail(parse_query("cobalt"), "claude", CLAUDE_ID)
        self.assertIsNotNone(detail)
        self.assertTrue(any(line["match"] and "cobalt" in line["text"]
                            for context in detail["contexts"] for line in context["lines"]))
        self.assertTrue(any("amber" in line["text"]
                            for context in detail["contexts"] for line in context["lines"]))

    def test_excluded_terms_apply_to_the_whole_session(self):
        append_jsonl(claude_path(self.root), [{"type": "assistant",
                                               "message": {"content": "forbidden elsewhere"}}])
        self.run_index()
        self.assertEqual(self.search("amber -forbidden"), [])

    def test_injected_claude_context_and_subagents_are_not_indexed(self):
        append_jsonl(claude_path(self.root), [{"type": "user", "isMeta": True,
                                               "message": {"content": "forbidden-injected-context"}}])
        subagent = claude_path(self.root).with_suffix("") / "subagents" / "agent-one.jsonl"
        append_jsonl(subagent, [{"type": "user", "message": {"content": "forbidden-subagent"}}])
        self.run_index()
        self.assertEqual(self.search("forbidden-injected-context"), [])
        self.assertEqual(self.search("forbidden-subagent"), [])


if __name__ == "__main__":
    unittest.main()
