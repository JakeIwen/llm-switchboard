import json
from pathlib import Path
import sys
import tempfile
import unittest

from llm_switchboard.codex_client import CodexClient
from llm_switchboard.codex_threads import CodexCatalog
from llm_switchboard.codex_trash import CodexTrash
from llm_switchboard.host_protocol import MAX_METADATA_BYTES
from llm_switchboard.host_transport import bounded_ssh
from llm_switchboard.mac_actions import MacActions
from llm_switchboard.mac_catalog import MacCatalog
from llm_switchboard.transcripts import TranscriptIndex
from llm_switchboard.trash import Trash
from fake_codex import FakeCodexDaemon, thread
from fakes import SID, SID2
from test_host_actions import queued


class HostTransportTests(unittest.TestCase):
    def test_full_duplex_and_output_limit(self):
        payload = "x" * 1000000
        result = bounded_ssh([sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"], input=payload)
        self.assertEqual(result.stdout.strip(), str(len(payload)))
        with self.assertRaisesRegex(ValueError, "output limit"):
            bounded_ssh([sys.executable, "-c", f"import sys; sys.stdout.write('x'*{MAX_METADATA_BYTES + 1})"], input="")

    def test_timeout_and_nonzero_exit(self):
        with self.assertRaises(TimeoutError):
            bounded_ssh([sys.executable, "-c", "import time; time.sleep(20)"], input="", timeout=.05)
        result = bounded_ssh([sys.executable, "-c", "import sys; sys.stderr.write('failure'); sys.exit(2)"], input="", check=False)
        self.assertEqual((result.returncode, result.stderr), (2, "failure"))


class MacCodexTests(unittest.TestCase):
    def test_metadata_and_allowlisted_edits_never_start_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = FakeCodexDaemon(root / "codex.sock", [thread(SID, str(root), name="Named", preview="PRIVATE"),
                                                          thread(SID2, str(root), preview="SECRET")])
            self.addCleanup(daemon.close)
            codex = CodexCatalog(CodexClient(root / "codex.sock"), metadata_only=True)
            catalog = MacCatalog(TranscriptIndex(root / "projects", metadata_only=True), codex, root / "codex", registry=lambda: [])
            rows = catalog.rows()
            # A named thread's prompt stays on the Mac; an unnamed one is titled by it.
            self.assertNotIn("PRIVATE", json.dumps(rows))
            self.assertEqual(next(r for r in rows if r["id"] == SID2)["title"], "SECRET")
            self.assertFalse(any("first_prompt" in r for r in rows))
            actions = MacActions(catalog, Trash(root / "trash"), CodexTrash(root / "codex-trash.json"))
            actions.execute(queued(tool="codex", body={"title": "Changed"}))
            result, upload = actions.execute(queued("fork", {"title": "Copy", "cwd": str(root)}, tool="codex"))
            self.assertIsNone(upload)
            self.assertIn(result["id"], daemon.threads)
            actions.execute(queued("delete", {}, tool="codex"))
            self.assertTrue(actions.trash_rows())
            actions.execute(queued("restore", {}, tool="codex"))
            self.assertTrue(any(r["id"] == SID for r in catalog.rows()))
            for kind in ("new", "enable", "restart", "move", "fork-pi"):
                with self.assertRaises(ValueError):
                    actions.execute(queued(kind, {}, tool="codex"))
            methods = {method for method, _ in daemon.calls}
            self.assertNotIn("thread/start", methods)
            self.assertNotIn("turn/start", methods)
            self.assertNotIn("thread/turns/list", methods)
