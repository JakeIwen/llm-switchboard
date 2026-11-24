"""The Mac link's paths and argvs in DeskConfig (docs/voice-desk.md,
"Processes, sockets and files" and "Module plan")."""

from pathlib import Path
import unittest

from llm_switchboard.desk.config import DeskConfig


class LinkConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = DeskConfig.from_env({}, home=Path("/Users/me"), repo=Path("/repo"))

    def test_sockets_and_run_locks_live_in_the_private_runtime_dir(self):
        run = Path("/Users/me/.config/llm-switchboard/desk/run")
        self.assertEqual(self.config.runtime_dir, run)
        for path, name in [(self.config.link_socket, "link.sock"), (self.config.hub_run_lock, "hub.lock"),
                           (self.config.relay_lock, "relay.lock")]:
            self.assertEqual(path, run / name)
        paths = [self.config.channel_socket, self.config.operator_socket, self.config.link_socket,
                 self.config.hub_run_lock, self.config.relay_lock]
        self.assertEqual(len(set(paths)), len(paths))

    def test_policy_files_live_beside_enrollment(self):
        self.assertEqual(self.config.link_signers_file, self.config.config_dir / "link-signers")
        self.assertEqual(self.config.remote_enrolled_file, self.config.config_dir / "enrolled-hosts.json")
        self.assertEqual(self.config.remote_enrolled_file.parent, self.config.enrolled_file.parent)

    def test_relay_logs_live_beside_the_mac_logs(self):
        logs = Path("/Users/me/Library/Logs/llm-switchboard")
        self.assertEqual(self.config.relay_log, logs / "desk-relay.log")
        self.assertEqual(self.config.relay_launch_log, logs / "desk-relay-launchd.log")

    def test_entry_points_run_the_checkout_interpreter_isolated(self):
        for argv, script in [(self.config.link_argv, "desk-link"), (self.config.relay_argv, "desk-relay"),
                             (self.config.channel_argv, "desk-channel")]:
            self.assertEqual(argv, ("/repo/.venv/bin/python", "-I", "-B", f"/repo/bin/{script}"))


if __name__ == "__main__":
    unittest.main()
