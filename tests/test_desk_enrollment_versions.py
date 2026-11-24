"""Mixed versions for Mac enrollment (docs/voice-desk.md, "Enrollment for
Mac sessions": host-qualified operations the phase 1 code refuses): the new
CLI and page against a phase 1 hub, exported from commit PHASE1_COMMIT and
run in a child interpreter (tests/desk_phase1.py). The new hub's refusal of
a phase 1 message carrying `host` is in test_desk_host_enrollment.py."""

import http.client
import json
import os
import unittest

from desk_fixtures import SESSION_A, client_context, desk_config, enroll_file, install_cert, make_home, run_desk_cli
from desk_phase1 import phase1_root, start_phase1_hub
from link_peer import MAC, write_host_enrollment
from test_desk_host_enrollment import SESSION_X, file_bytes, mac_lock
from test_desk_link_channels import SESSION_M

SHA256_FINGERPRINT = "SHA256:" + "A" * 43
OLDER_HUB = "the running desk hub is older and can't enroll Mac sessions; nothing changed"


def child_env(home) -> dict:
    """The environment `run_desk_cli` gives the new CLI, so both find one hub."""
    return {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": "", "XDG_STATE_HOME": "", "XDG_RUNTIME_DIR": "",
            "LLM_SWITCHBOARD_DESK_PORT": "0"}


class Phase1HubTest(unittest.TestCase):
    def setUp(self):
        self.root = phase1_root(self)
        self.home = make_home(self)
        self.config = desk_config(self.home)
        enroll_file(self.config, SESSION_A)
        write_host_enrollment(self.config.remote_enrolled_file, {SESSION_M: MAC})

    def test_new_cli_against_phase1_operator_handler(self):
        lock = mac_lock(self.config, SESSION_M, SHA256_FINGERPRINT)
        start_phase1_hub(self, self.root, child_env(self.home), http=False)
        files = (self.config.enrolled_file, self.config.remote_enrolled_file, self.config.lock_file)
        before = file_bytes(*files)
        for args in (("enroll", SESSION_X, "--host", MAC), ("unenroll", SESSION_M, "--host", MAC)):
            with self.subTest(args=args):
                done = run_desk_cli(self.home, *args, input="")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn(OLDER_HUB, done.stderr)
        # The phase 1 hub finds no Mac lock it can list (its store refuses
        # the file), so the override refuses and changes nothing.
        done = run_desk_cli(self.home, "clear-stopped", SESSION_M, lock.lock_id, "--confirm", lock.lock_id, input="")
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertEqual(file_bytes(*files), before)
        # The phase 1 messages still work against it.
        done = run_desk_cli(self.home, "enroll", SESSION_X, input="")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(SESSION_X, json.loads(self.config.enrolled_file.read_text())["sessions"])

    def test_host_routes_404_on_phase1_http(self):
        cert = install_cert(self.config)
        port = start_phase1_hub(self, self.root, child_env(self.home), http=True)
        before = file_bytes(self.config.enrolled_file, self.config.remote_enrolled_file)
        for path in ("/desk/api/enroll-host", "/desk/api/unenroll-host"):
            with self.subTest(path=path):
                conn = http.client.HTTPSConnection("127.0.0.1", port, context=client_context(cert), timeout=30)
                try:
                    conn.request("POST", path, body=json.dumps({"session_id": SESSION_X, "host": MAC}),
                                 headers={"Host": f"localhost:{port}", "Origin": f"https://localhost:{port}",
                                          "Content-Type": "application/json"})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 404)
                    response.read()
                finally:
                    conn.close()
        self.assertEqual(file_bytes(self.config.enrolled_file, self.config.remote_enrolled_file), before)
