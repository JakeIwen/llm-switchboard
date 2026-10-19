import base64
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from llm_switchboard.host_agent import HostAgent
from llm_switchboard.mac_action_runner import MacActionRunner
from llm_switchboard.host_authorize import authorize
from llm_switchboard.host_exchange import exchange
from llm_switchboard.host_launchd import job
from llm_switchboard.host_protocol import MAX_METADATA_BYTES, MAX_ROWS
from llm_switchboard.hosts import Hosts
from llm_switchboard.mac_catalog import MacCatalog, codex_disk_rows
from llm_switchboard.mac_registry import mac_live_processes
from llm_switchboard.transcripts import TranscriptIndex
from fakes import Clock, SID

ROOT = Path(__file__).resolve().parents[1]


def sample(**changes):
    return dict(id=SID, tool="claude", title="Mac task", cwd="/Users/jacobr/dev/a", mtime=100, size=42,
                state="closed", mutable=True, **changes)


def key():
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes(range(32))
    return b"ssh-ed25519 " + base64.b64encode(blob) + b" test\n"


class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.hosts = Hosts(self.root / "hosts", self.clock)

    def test_snapshot_age_privacy_and_namespacing(self):
        payload = {"version": 1, "host": "pretender", "rows": [sample(first_prompt="secret", screen="secret", messages=["secret"])]}
        self.hosts.exchange("m4mac", payload)
        snap = self.hosts.snapshot()
        row = snap["sessions"][0]
        self.assertEqual(row["id"], f"m4mac:{SID}")
        self.assertEqual(row["host"], "m4mac")
        self.assertNotIn("secret", json.dumps(snap))
        self.assertIn("claude --resume", row["resume_cmd"])
        self.clock.t += 34.99
        self.assertTrue(self.hosts.snapshot()["hosts"][0]["online"])
        self.clock.t += .01
        self.assertFalse(self.hosts.snapshot()["hosts"][0]["online"])
        self.assertEqual(self.hosts.snapshot()["hosts"][0]["last_seen"], 10000)

    def test_snapshot_skips_a_temp_file_written_beside_a_host(self):
        self.hosts.exchange("m4mac", {"version": 1, "rows": [sample()]})
        (self.root / "hosts" / ".m4mac-k2j4x9.json").write_text("{")
        snap = self.hosts.snapshot()
        self.assertEqual([h["host"] for h in snap["hosts"]], ["m4mac"])
        self.assertFalse((self.root / "hosts" / ".m4mac-k2j4x9.lock").exists())

    def test_exchange_invalid_data_and_cap(self):
        for raw in [b"x" * (MAX_METADATA_BYTES + 1), b"[]",
                    json.dumps({"version": 1, "rows": [sample()] * (MAX_ROWS + 1)}).encode(),
                    json.dumps({"version": 1, "rows": [], "uploads": [{}]}).encode()]:
            with self.assertRaises(ValueError):
                exchange(io.BytesIO(raw), io.StringIO(), "m4mac", self.hosts)
        for changes in [{"id": "../bad"}, {"id": SID + "\n"}, {"id": []}, {"mtime": 10**1000}, {"title": "x\nhello"}, {"cwd": "relative"},
                        {"size": True}, {"mtime": float("nan")}, {"state": "arbitrary"}, {"mutable": 1}]:
            with self.assertRaises(ValueError):
                self.hosts.exchange("m4mac", {"version": 1, "rows": [{**sample(), **changes}]})
        with self.assertRaises(ValueError):
            self.hosts.exchange("../bad", {"version": 1, "rows": []})
        out = io.StringIO()
        exchange(io.BytesIO(b'{"version":1,"rows":[]}'), out, "m4mac", self.hosts)
        self.assertEqual(json.loads(out.getvalue()), {"version": 1, "actions": []})

    def test_authorize_replaces_only_its_marker(self):
        ssh = self.root / ".ssh"
        ssh.mkdir()
        path = ssh / "authorized_keys"
        original = b'# untouched\r\nssh-ed25519 OTHER other user\n'
        path.write_bytes(original)
        authorize("m4mac", key(), self.root, ROOT / "bin/llm-switchboard")
        first = path.read_bytes()
        authorize("m4mac", key(), self.root, ROOT / "bin/llm-switchboard")
        self.assertEqual(path.read_bytes(), first)
        self.assertTrue(first.startswith(original))
        self.assertIn(b'restrict,command="', first)
        self.assertIn(b'host-exchange m4mac" ssh-ed25519', first)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for bad in [b'command="bad" ' + key(), key() + key(), b"ssh-rsa !!!!\n", b"ssh-ed25519 AAAA\n"]:
            with self.assertRaises(ValueError):
                authorize("m4mac", bad, self.root)
        self.assertEqual(path.read_bytes(), first)

    def test_metadata_does_not_fall_back_to_message(self):
        project = self.root / "projects" / "folder"
        project.mkdir(parents=True)
        path = project / f"{SID}.jsonl"
        path.write_text(json.dumps({"type": "user", "cwd": "/Users/jacobr/dev/a", "message": {"content": "secret"}}, separators=(",", ":")) + "\n")
        codex = Mock(error="offline")
        codex.threads.return_value = []
        rows = MacCatalog(TranscriptIndex(project.parent, metadata_only=True), codex, self.root, registry=lambda: []).rows()
        self.assertEqual(rows[0]["title"], "(untitled)")
        self.assertNotIn("secret", json.dumps(rows))
        self.assertNotIn("first_prompt", rows[0])

    def test_codex_fallback_is_metadata_only(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        (sessions / "rollout.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": SID, "cwd": "/Users/jacobr/dev/a"}}) + '\n{"secret":"prompt"}\n')
        (self.root / "session_index.jsonl").write_text(json.dumps({"id": SID, "thread_name": "A title"}) + "\n")
        rows = codex_disk_rows(self.root)
        self.assertEqual(rows[0]["title"], "A title")
        self.assertFalse(rows[0]["mutable"])
        self.assertNotIn("secret", json.dumps(rows))

    def test_mac_liveness_and_no_process_launch(self):
        (self.root / "1.json").write_text(json.dumps({"pid": 1, "sessionId": SID, "status": "busy"}))
        kill = Mock()
        found = mac_live_processes(self.root, kill=kill)
        self.assertEqual(found[0].session_id, SID)
        kill.assert_called_once_with(1, 0)
        kill.side_effect = ProcessLookupError()
        self.assertEqual(mac_live_processes(self.root, kill=kill), [])
        catalog = Mock()
        catalog.rows.return_value = [sample()]
        run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"version":1,"actions":[]}', ""))
        executor = Mock()
        executor.trash_rows.return_value = []
        runner = MacActionRunner(executor, self.root / "ledger.json")
        HostAgent("vanpi", "m4mac", self.root / "key", catalog, run, executor, runner).cycle()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "ssh")
        self.assertIn("ControlMaster=auto", run.call_args.args[0])

    def test_real_exchange_cycle_through_fake_ssh(self):
        pi = self.root / "pi"
        pi.mkdir()
        bindir = self.root / "bin"
        bindir.mkdir()
        ssh = bindir / "ssh"
        ssh.write_text(f'#!/bin/sh\nexec env HOME="{pi}" XDG_DATA_HOME= "{sys.executable}" -m llm_switchboard host-exchange m4mac\n')
        ssh.chmod(0o700)
        env = {**os.environ, "HOME": str(self.root), "XDG_DATA_HOME": "", "XDG_CONFIG_HOME": "",
               "PYTHONPATH": str(ROOT), "PATH": str(bindir) + ":" + os.environ["PATH"], "LLM_SWITCHBOARD_CODEX_SOCKET": ""}
        result = subprocess.run([sys.executable, "-m", "llm_switchboard", "host-agent", "--once"],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((pi / ".local/share/llm-switchboard/hosts/m4mac.json").read_text())
        self.assertEqual(state["rows"], [])
        self.assertGreater(state["last_seen"], 0)

    def test_launchd_uses_selected_python_and_no_server(self):
        config = job("vanpi", "m4mac", self.root / "key", self.root, "/opt/homebrew/bin/python3", ROOT)
        self.assertEqual(config["ProgramArguments"][0], "/opt/homebrew/bin/python3")
        self.assertIn("host-agent", config["ProgramArguments"])
        self.assertNotIn("serve", config["ProgramArguments"])
        self.assertTrue(config["KeepAlive"])
