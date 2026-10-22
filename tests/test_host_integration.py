import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from llm_switchboard.hosts import Hosts
from llm_switchboard.store import Store
from llm_switchboard.supervisor import Supervisor
from llm_switchboard.transcripts import TranscriptIndex
from llm_switchboard.trash import Trash
from llm_switchboard.web import App, make_server
from fakes import Clock, FakeConnectivity, FakeIndex, FakeTmux, SID, transcript
from test_hosts import ROOT, sample
from test_host_actions import write_transcript


class HostIntegrationTests(unittest.TestCase):
    def test_fake_ssh_performs_closed_session_rename_and_reports_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mac, pi = root / "mac", root / "pi"
            mac.mkdir()
            pi.mkdir()
            cwd = mac / "work"
            cwd.mkdir()
            projects = mac / ".claude/projects"
            write_transcript(projects, cwd)
            hosts = Hosts(pi / ".local/share/llm-switchboard/hosts")
            hosts.exchange("mac", {"version": 1, "rows": [{**sample(), "cwd": str(cwd)}]})
            hosts.queue("mac", SID, "rename", {"title": "Done through SSH"})
            bindir = root / "bin"
            bindir.mkdir()
            ssh = bindir / "ssh"
            ssh.write_text(f'#!/bin/sh\nexec env HOME="{pi}" XDG_DATA_HOME= "{sys.executable}" -m llm_switchboard host-exchange mac\n')
            ssh.chmod(0o700)
            env = {**os.environ, "HOME": str(mac), "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", "PYTHONPATH": str(ROOT),
                   "PATH": str(bindir) + ":" + os.environ["PATH"], "LLM_SWITCHBOARD_CODEX_SOCKET": ""}
            result = subprocess.run([sys.executable, "-m", "llm_switchboard", "host-agent", "--once"],
                                    env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(TranscriptIndex(projects).get(SID).title, "Done through SSH")
            self.assertEqual(hosts.snapshot()["actions"][0]["state"], "done")
            self.assertNotIn("PRIVATE TEXT", json.dumps(hosts.snapshot()))
            self.assertFalse((mac / ".config/llm-switchboard/sessions.json").exists())

    def test_host_routes_cannot_reach_local_sessions_or_peek(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, tmux, index = Store(root / "sessions.json"), FakeTmux(), FakeIndex(transcript())
            hosts = Hosts(root / "hosts")
            hosts.exchange("mac", {"version": 1, "rows": [sample()]})
            supervisor = Supervisor(store, index, tmux, FakeConnectivity(), registry=lambda: [], clock=Clock())
            app = App(store, index, supervisor, tmux, Trash(root / "trash"), registry=lambda: [], hosts=hosts)
            server = make_server(app, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def call(path, body=None, content_type="application/json"):
                    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
                                                 data=None if body is None else json.dumps(body).encode(),
                                                 headers={"Content-Type": content_type})
                    try:
                        with urllib.request.urlopen(req, timeout=5) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as e:
                        return e.code, json.load(e)
                rows = call("/api/sessions")[1]["sessions"]
                self.assertEqual({r["id"] for r in rows}, {SID, f"mac:{SID}"})
                remote = f"/api/hosts/mac/sessions/{SID}"
                self.assertEqual(call(remote + "/enable", {})[0], 400)
                self.assertEqual(call(remote + "/screen")[0], 404)
                self.assertEqual(call(remote + "/messages")[0], 404)
                self.assertEqual(call(remote + "/rename", {"title": "x"}, "text/plain")[0], 415)
                self.assertEqual(call(f"/api/sessions/mac:{SID}/enable", {})[0], 400)
                self.assertEqual(call(remote + "/rename", {"title": "x"})[1]["state"], "queued")
                self.assertEqual(store.load(), {})
                self.assertEqual(tmux.started, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
