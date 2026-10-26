"""Transcript scanning, the live-process registry and the store, against temp folders."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from llm_switchboard.live_registry import live_processes
from llm_switchboard.paths import MANAGED_ENV_VAR
from llm_switchboard.store import Store
from llm_switchboard.transcripts import TranscriptIndex

from fakes import SID, SID2


def write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))


def user(text, **extra):
    return {"type": "user", "cwd": "/home/pi/dev/x", "message": {"role": "user", "content": text}, **extra}


class TranscriptIndexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.index = TranscriptIndex(self.root)

    def test_first_real_prompt_is_the_fallback_title(self):
        write_jsonl(self.root / "p" / f"{SID}.jsonl", [
            user("<local-command-caveat>x</local-command-caveat>", isMeta=True),
            user("<command-name>/model</command-name>"),
            user([{"type": "tool_result", "content": "x"}]),
            user("  make the   dashboard faster "),
        ])
        [t] = self.index.all()
        self.assertEqual(t.title, "make the dashboard faster")
        self.assertEqual(t.cwd, "/home/pi/dev/x")

    def test_custom_title_beats_ai_title(self):
        write_jsonl(self.root / "p" / f"{SID}.jsonl", [
            user("hello"),
            {"type": "ai-title", "aiTitle": "AI name"},
            {"type": "custom-title", "customTitle": "my name"},
            {"type": "ai-title", "aiTitle": "later AI name"},
        ])
        self.assertEqual(self.index.get(SID).title, "my name")

    def test_headless_runs_are_marked_until_someone_types(self):
        cases = {
            "sdk-cli": (["sdk-cli", "sdk-cli"], True),
            "resumed in a terminal": (["sdk-cli", "cli"], False),
            "resumed headless": (["cli", "sdk-cli"], False),
            "older Claude Code": ([None], False),
        }
        for name, (entrypoints, headless) in cases.items():
            with self.subTest(name):
                path = self.root / name / f"{SID}.jsonl"
                write_jsonl(path, [user("hi", **({"entrypoint": e} if e else {})) for e in entrypoints])
                self.assertEqual(TranscriptIndex(path.parent.parent).get(SID).headless, headless)
                path.unlink()

    def test_empty_and_non_session_files_are_skipped(self):
        write_jsonl(self.root / "p" / f"{SID}.jsonl", [{"type": "file-history-snapshot"}])
        write_jsonl(self.root / "p" / "notes.jsonl", [user("hi")])
        self.assertEqual(self.index.all(), [])

    def test_changed_file_is_reread(self):
        path = self.root / "p" / f"{SID}.jsonl"
        write_jsonl(path, [user("one")])
        self.assertEqual(self.index.get(SID).title, "one")
        write_jsonl(path, [user("one"), {"type": "ai-title", "aiTitle": "two"}])
        os.utime(path, ns=(1, 2_000_000_000_000))
        self.assertEqual(self.index.get(SID).title, "two")

    def test_prefix_resolution(self):
        write_jsonl(self.root / "p" / f"{SID}.jsonl", [user("a")])
        write_jsonl(self.root / "q" / f"{SID2}.jsonl", [user("b")])
        self.assertEqual([t.session_id for t in self.index.resolve("c969")], [SID])


class LiveRegistryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = Path(tmp.name) / "sessions"
        self.proc = Path(tmp.name) / "proc"
        self.registry.mkdir()

    def add(self, pid, sid, start, environ=b"", actual_start=None):
        (self.registry / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": sid, "cwd": "/w", "status": "idle", "procStart": str(start)}))
        d = self.proc / str(pid)
        d.mkdir(parents=True)
        fields = ["S"] + ["0"] * 18 + [str(actual_start or start)]
        (d / "stat").write_text(f"{pid} (claude code) " + " ".join(fields) + " 0 0\n")
        (d / "environ").write_bytes(environ)
        (d / "status").write_text("Name:\tclaude\nVmRSS:\t  204800 kB\n")

    def test_reads_live_entries_and_marks_ours(self):
        self.add(10, SID, 555, environ=f"A=1\0{MANAGED_ENV_VAR}={SID}\0".encode())
        self.add(11, SID2, 777)
        procs = {p.pid: p for p in live_processes(self.registry, self.proc)}
        self.assertEqual(procs[10].managed_for, SID)
        self.assertIsNone(procs[11].managed_for)
        self.assertEqual(procs[10].rss_kb, 204800)

    def test_ignores_dead_and_reused_pids(self):
        self.add(12, SID, 100, actual_start=999)
        (self.registry / "13.json").write_text(json.dumps({"pid": 13, "sessionId": SID}))
        self.assertEqual(live_processes(self.registry, self.proc), [])


class StoreTest(unittest.TestCase):
    def test_round_trip_keeps_other_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "nested" / "sessions.json")
            self.assertEqual(store.load(), {})
            store.set_enabled(SID, True)
            store.set_name(SID, "  van  ")
            store.set_enabled(SID2, True)
            store.set_enabled(SID2, False)
            loaded = store.load()
            self.assertTrue(loaded[SID].enabled)
            self.assertEqual(loaded[SID].name, "van")
            self.assertFalse(loaded[SID2].enabled)

    def test_rejects_non_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                Store(Path(tmp) / "s.json").set_enabled("../../etc", True)


if __name__ == "__main__":
    unittest.main()
