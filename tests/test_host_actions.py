import base64
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import Mock

from llm_switchboard.codex_trash import CodexTrash
from llm_switchboard.conflict import Conflict
from llm_switchboard.host_actions_protocol import MAX_EXCHANGE_BYTES, MAX_UPLOAD_BYTES, action
from llm_switchboard.host_exchange import exchange
from llm_switchboard.host_import import HostImporter, mapped_folder
from llm_switchboard.host_protocol import MAX_METADATA_BYTES
from llm_switchboard.hosts import Hosts
from llm_switchboard.mac_actions import MacActions
from llm_switchboard.mac_action_runner import MacActionRunner
from llm_switchboard.mac_catalog import MacCatalog
from llm_switchboard.mac_uploads import MacUploads
from llm_switchboard.names import Names
from llm_switchboard.transcripts import TranscriptIndex
from llm_switchboard.trash import Trash
from fakes import Clock, SID, SID2
from test_hosts import sample


def queued(kind="rename", body=None, sid=SID, **changes):
    return {"id": str(uuid.uuid4()), "session_id": sid, "tool": "claude", "action": kind,
            "source_cwd": "/Users/jacobr/dev/a",
            "body": body if body is not None else {"title": "Renamed"}, "ttl": 60, **changes}


def write_transcript(projects, cwd, sid=SID, title="Mac task"):
    project = projects / "source"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{sid}.jsonl"
    records = [{"type": "user", "sessionId": sid, "cwd": str(cwd), "message": {"content": "PRIVATE TEXT"}},
               {"type": "custom-title", "sessionId": sid, "customTitle": title}]
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    return path


class HostQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.importer = Mock()
        self.hosts = Hosts(self.root / "hosts", self.clock, self.importer)
        self.payload = {"version": 1, "rows": [sample()]}
        self.hosts.exchange("m4mac", self.payload)

    def test_states_ttl_and_no_redelivery(self):
        q = self.hosts.queue("m4mac", SID, "rename", {"title": "Renamed"})
        self.assertEqual(q["state"], "queued")
        self.clock.t += 20
        received = self.hosts.exchange("m4mac", self.payload)["actions"]
        self.assertEqual(received[0]["ttl"], 40)
        self.assertEqual(self.hosts.snapshot()["actions"][0]["state"], "sent")
        self.assertEqual(self.hosts.exchange("m4mac", self.payload)["actions"], [])
        reply = self.hosts.exchange("m4mac", {**self.payload, "results": [{"id": q["action_id"], "ok": True, "message": "done"}]})
        self.assertEqual(reply["acknowledged"], [q["action_id"]])
        self.assertEqual(self.hosts.snapshot()["actions"][0]["state"], "done")
        self.hosts.queue("m4mac", SID, "delete", {})
        self.clock.t += 60
        self.assertEqual(self.hosts.exchange("m4mac", self.payload)["actions"], [])
        self.assertEqual(self.hosts.snapshot()["actions"][-1]["state"], "expired")
        self.assertIn("nothing ran", self.hosts.snapshot()["actions"][-1]["message"])

    def test_sent_without_result_expires_unknown(self):
        self.hosts.queue("m4mac", SID, "delete", {})
        self.hosts.exchange("m4mac", self.payload)
        self.clock.t += 60
        state = self.hosts.snapshot()["actions"][0]
        self.assertEqual(state["state"], "expired")
        self.assertIn("outcome unknown", state["message"])

    def test_offline_list_only_and_allow_list(self):
        for kind, body in [("new", {}), ("keep", {}), ("restart", {}), ("fork", {"title": "new", "cwd": "/tmp", "keep": True})]:
            with self.assertRaises(ValueError):
                self.hosts.queue("m4mac", SID, kind, body)
        self.clock.t += 35
        with self.assertRaises(Conflict):
            self.hosts.queue("m4mac", SID, "delete", {})
        self.hosts.exchange("m4mac", {"version": 1, "rows": [{**sample(), "tool": "codex", "mutable": False}]})
        with self.assertRaises(Conflict):
            self.hosts.queue("m4mac", SID, "rename", {"title": "x"})

    def test_upload_only_while_waiting_and_correlated(self):
        self.assertEqual(self.hosts.input_limit("m4mac"), MAX_METADATA_BYTES)
        q = self.hosts.queue("m4mac", SID, "fork-pi", {"title": "Copy"})
        self.assertEqual(self.hosts.input_limit("m4mac"), MAX_METADATA_BYTES)
        self.hosts.exchange("m4mac", self.payload)
        self.assertEqual(self.hosts.input_limit("m4mac"), MAX_EXCHANGE_BYTES)
        self.importer.receive.return_value = "Copied, not running"
        self.hosts.exchange("m4mac", {**self.payload, "results": [{"id": q["action_id"], "ok": True, "message": ""}],
                                      "uploads": [{"action_id": q["action_id"], "transcript": ""}]})
        self.importer.receive.assert_called_once()
        self.assertEqual(self.hosts.snapshot()["actions"][0]["state"], "done")
        self.assertEqual(self.hosts.input_limit("m4mac"), MAX_METADATA_BYTES)
        with self.assertRaises(ValueError):
            self.hosts.exchange("m4mac", {**self.payload, "uploads": [{"action_id": str(uuid.uuid4()), "transcript": ""}]})

    def test_import_refusal_is_visible_failure(self):
        q = self.hosts.queue("m4mac", SID, "fork-pi", {"title": "Copy"})
        self.hosts.exchange("m4mac", self.payload)
        self.importer.receive.side_effect = ValueError("Mapped folder vanished")
        self.hosts.exchange("m4mac", {**self.payload, "results": [{"id": q["action_id"], "ok": True, "message": ""}],
                                      "uploads": [{"action_id": q["action_id"], "transcript": ""}]})
        result = self.hosts.snapshot()["actions"][0]
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["message"], "Mapped folder vanished")

    def test_result_for_a_forgotten_action_is_acknowledged_and_the_push_lands(self):
        forgotten = str(uuid.uuid4())
        renamed = {**sample(), "title": "Renamed on the Mac"}
        reply = self.hosts.exchange("m4mac", {**self.payload, "rows": [renamed],
                                              "results": [{"id": forgotten, "ok": True, "message": "done"}]})
        self.assertEqual(reply["acknowledged"], [forgotten])
        self.assertEqual(self.hosts.snapshot()["sessions"][0]["title"], "Renamed on the Mac")

    def test_prevalidate_all_results_before_import(self):
        q = self.hosts.queue("m4mac", SID, "fork-pi", {"title": "Copy"})
        self.hosts.exchange("m4mac", self.payload)
        with self.assertRaises(ValueError):
            self.hosts.exchange("m4mac", {**self.payload, "results": [
                {"id": q["action_id"], "ok": True, "message": ""}, {"id": q["action_id"], "ok": False, "message": ""}],
                "uploads": [{"action_id": q["action_id"], "transcript": ""}]})
        self.importer.receive.assert_not_called()


class MacActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.projects = self.root / "projects"
        self.path = write_transcript(self.projects, self.cwd)
        self.index = TranscriptIndex(self.projects, metadata_only=True)
        codex = Mock(error="offline")
        codex.threads.return_value = []
        self.catalog = MacCatalog(self.index, codex, self.root / "codex", registry=lambda: [])
        self.notify = Mock()
        self.uploads = MacUploads(self.root / "uploads.json", self.root / "logs", run=self.notify)
        self.executor = MacActions(self.catalog, Trash(self.root / "trash"), CodexTrash(self.root / "codex-trash.json"), self.uploads)
        self.runner = MacActionRunner(self.executor, self.root / "ledger.json")

    def test_edits_fork_move_delete_and_restore_without_launch(self):
        self.executor.execute(queued())
        self.assertEqual(self.index.get(SID).title, "Renamed")
        outcome, _ = self.executor.execute(queued("fork", {"cwd": str(self.cwd), "title": "Copy"}))
        self.assertIsNotNone(self.index.get(outcome["id"]))
        target = self.root / "target"
        target.mkdir()
        self.executor.execute(queued("move", {"cwd": str(target)}))
        self.assertEqual(self.index.get(SID).cwd, str(target))
        self.executor.execute(queued("delete", {}))
        self.assertIsNone(self.index.get(SID))
        self.assertEqual(self.executor.trash_rows()[0]["id"], SID)
        self.executor.execute(queued("restore", {}))
        self.assertIsNotNone(self.index.get(SID))
        self.assertFalse((self.root / "sessions.json").exists())

    def test_own_validation_refuses_paths_titles_ids_and_forbidden_actions(self):
        for raw in [queued("restart", {}), queued("new", {}), queued("fork", {"title": "Copy", "cwd": str(self.cwd), "keep": False}),
                    queued(sid="../x"), queued(body={"title": "a\nb"}), queued(body={"title": ""}),
                    queued("move", {"cwd": "relative"}), queued("move", {"cwd": str(self.root / "missing")})]:
            with self.assertRaises((ValueError, LookupError)):
                self.executor.execute(raw)
        self.catalog.registry = lambda: [Mock(session_id=SID)]
        for kind, body in [("rename", {"title": "New"}), ("delete", {}), ("fork-pi", {"title": "Copy"}),
                           ("fork", {"title": "Copy", "cwd": str(self.cwd)})]:
            with self.assertRaises(Conflict):
                self.executor.execute(queued(kind, body))
        self.assertEqual(self.index.get(SID).title, "Mac task")

    def test_runner_dedup_and_expiration(self):
        raw = queued()
        results, _ = self.runner.run([raw])
        self.assertTrue(results[0]["ok"])
        size = self.path.stat().st_size
        again = MacActionRunner(self.executor, self.root / "ledger.json")
        again.run([raw])
        self.assertEqual(size, self.path.stat().st_size)
        expired, _ = again.run([queued(body={"title": "Too late"}, ttl=1)], transport_elapsed=2)
        self.assertFalse(expired[0]["ok"])
        self.assertIn("expired", expired[0]["message"])
        self.assertEqual(self.index.get(SID).title, "Renamed")
        again.acknowledge([r["id"] for r in again.pending()])
        self.assertEqual(again.pending(), [])

    def test_upload_notification_quota_and_lost_ack(self):
        item = queued("fork-pi", {"title": "Copy"}, source_cwd=str(self.cwd))
        result, uploads = self.runner.run([item])
        self.assertTrue(result[0]["ok"])
        self.assertIn(b"PRIVATE TEXT", base64.b64decode(uploads[0]["transcript"]))
        argv = self.notify.call_args.args[0]
        self.assertEqual(argv[0], "osascript")
        self.assertIn("on run argv", argv[2])
        self.assertNotIn(SID, argv[2])
        self.assertIn(SID, argv[3])
        self.assertIn(SID, (self.root / "logs/uploads.log").read_text())
        # Durable pending record cannot trigger a second transcript upload after a crash.
        self.assertFalse(self.runner.pending()[0]["ok"])
        result, uploads = self.runner.run([item])
        self.assertFalse(result[0]["ok"])
        self.assertEqual(uploads, [])
        for _ in range(9):
            self.uploads.export(self.index.get(SID), str(uuid.uuid4()))
        restarted = MacUploads(self.root / "uploads.json", self.root / "logs", run=self.notify)
        with self.assertRaisesRegex(ValueError, "10 per hour"):
            restarted.export(self.index.get(SID), str(uuid.uuid4()))
        self.assertEqual(self.notify.call_count, 10)

    def test_import_rewrites_without_starting_and_refuses_missing_mapping(self):
        pi_home = self.root / "pi"
        target = pi_home / "dev/a"
        target.mkdir(parents=True)
        pi_projects = pi_home / "projects"
        importer = HostImporter(pi_projects, pi_home, Names())
        with self.assertRaisesRegex(ValueError, "does not exist"):
            importer.check("/Users/jacobr/missing", "Copy")
        with self.assertRaises(ValueError):
            mapped_folder("/Users/other/dev/a", pi_home)
        source = write_transcript(self.root / "export", "/Users/jacobr/dev/a")
        item = {"session_id": SID, "cwd": "/Users/jacobr/dev/a", "body": {"title": "Pi copy"}}
        message = importer.receive(item, {"transcript": base64.b64encode(source.read_bytes()).decode()})
        self.assertIn("not kept running", message)
        copy = TranscriptIndex(pi_projects).all()[0]
        self.assertNotEqual(copy.session_id, SID)
        self.assertEqual(copy.cwd, str(target))
        self.assertEqual(copy.title, "Pi copy")
        self.assertNotIn(f'"sessionId":"{SID}"', copy.path.read_text())
        self.assertTrue(source.exists())
        with self.assertRaises(ValueError):
            importer.receive(item, {"transcript": "not base64!"})

    def test_restore_refuses_name_collision(self):
        self.executor.execute(queued("delete", {}))
        write_transcript(self.projects, self.cwd, SID2, "Mac task")
        with self.assertRaises(Conflict):
            self.executor.execute(queued("restore", {}))
