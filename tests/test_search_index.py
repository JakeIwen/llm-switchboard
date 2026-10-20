import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from llm_switchboard.search_index import TranscriptSearch
from llm_switchboard.search_indexer import run_index
from llm_switchboard.search_query import parse_query

from search_fixture import (CLAUDE_ID, CODEX_ID, append_jsonl, claude_path, claude_records,
                            codex_item_records, codex_path, codex_records)


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
        self.assertEqual(len(self.search("by:reply cobalt")), 1)
        self.assertEqual(len(self.search("by:me gold")), 1)
        self.assertEqual(len(self.search("tool:codex ochre")), 1)
        self.assertEqual(len(self.search("by:me ochre")), 1)
        self.assertEqual(self.search("by:reply ochre"), [])
        self.assertEqual(len(self.search("by:reply teal")), 1)
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
                                               "message": {"content": [{"type": "text", "text": "forbidden elsewhere"}]}}])
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

    def test_a_fork_keeps_its_own_id_despite_copied_ancestor_lines(self):
        fork_id = "44444444-4444-4444-4444-444444444444"
        copied_meta = codex_records()[0]
        append_jsonl(codex_path(self.root, fork_id), [
            {"type": "session_meta", "payload": {"id": fork_id, "forked_from_id": CODEX_ID, "cwd": "/work/fork"}},
            copied_meta,
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "lilac fork reply"}},
        ])
        claude_fork = "55555555-5555-5555-5555-555555555555"
        append_jsonl(claude_path(self.root, claude_fork), [
            {**claude_records()[0], "message": {"content": "russet copied line"}},
        ])
        self.run_index()
        self.assertEqual([(r["tool"], r["id"]) for r in self.search("lilac")], [("codex", fork_id)])
        self.assertEqual([r["id"] for r in self.search("ochre")], [CODEX_ID])
        self.assertEqual([r["id"] for r in self.search("russet")], [claude_fork])
        self.assertEqual([r["id"] for r in self.search("amber")], [CLAUDE_ID])

    def test_only_prompts_and_replies_are_indexed(self):
        for word in ("violet", "ivory", "scarlet",  # Claude thinking, tool input, tool result
                     "khaki", "secretblobword", "sepia", "umber", "beige"):  # Codex equivalents
            with self.subTest(word=word):
                self.assertEqual(self.search(word), [])
        # Codex writes each message as a response item and as an event; one is indexed.
        [codex] = self.search("teal")
        self.assertEqual(codex["count"], 1)

    def test_reads_wait_out_a_brief_lock_instead_of_reporting_no_index(self):
        # Closing the last connection checkpoints the WAL under an exclusive
        # lock, which a slow disk can hold for a while; exclusive locking mode
        # holds the same lock on purpose.
        writer = sqlite3.connect(self.index_file, check_same_thread=False)
        writer.execute("PRAGMA locking_mode=EXCLUSIVE")
        writer.execute("UPDATE index_meta SET value=value WHERE key='state'")
        writer.commit()
        release = threading.Timer(0.3, writer.close)
        release.start()
        self.addCleanup(release.cancel)
        index = TranscriptSearch(self.index_file)
        self.assertIsNotNone(index.status()["last_updated"])
        self.assertEqual(len(index.search(parse_query("amber"))), 1)

    def test_newer_codex_rollouts_index_completed_message_items(self):
        sid = "66666666-6666-6666-6666-666666666666"
        append_jsonl(codex_path(self.root, sid), codex_item_records(sid))
        self.run_index()
        self.assertEqual([r["id"] for r in self.search("by:me mauve")], [sid])
        self.assertEqual([r["id"] for r in self.search("by:reply cerise")], [sid])
        self.assertEqual(self.search("plum"), [])

    def test_codex_subagent_rollouts_are_not_indexed_or_listed(self):
        sub = "77777777-7777-7777-7777-777777777777"
        append_jsonl(codex_path(self.root, sub), [
            {"type": "session_meta", "payload": {
                "session_id": CODEX_ID, "id": sub, "cwd": "/work/codex",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": CODEX_ID}}}}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "azure brief from the parent"}},
        ])
        self.run_index()
        self.assertEqual(self.search("azure"), [])
        append_jsonl(codex_path(self.root, sub), [
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "azure later reply"}}])
        self.run_index()
        self.assertEqual(self.search("azure"), [])
        self.assertEqual(TranscriptSearch(self.index_file).status()["sessions"], 2)

if __name__ == "__main__":
    unittest.main()
