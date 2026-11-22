"""The startup acknowledgement's record of attempted views: removed only on
proof (nothing typed, or its claude proven gone), never on an unreadable
process and never by eviction; a full table refuses new acknowledgements."""

import unittest
from unittest import mock

from desk_fixtures import SESSION_A, SESSION_B, wait_until
from test_desk_startup_ack import StartupCase

from llm_switchboard.desk import startup_ack
from llm_switchboard.desk.model import ChannelProcess
from llm_switchboard.desk.startup_identity import StartupRefusal


class AttemptRecordTest(StartupCase):
    def acknowledged(self, session_id):
        """A launch whose one Enter was sent while its warning stays on screen."""
        launch = self.launch(session_id)
        (launch.state / "ignore-enter").touch()
        digest = self.digest_for(launch)
        self.assertEqual(self.ack(session_id, digest)[0], 200)
        wait_until(lambda: launch.typed() == b"\r")
        return launch, digest

    def test_an_unreadable_process_keeps_its_record(self):
        first, digest = self.acknowledged(SESSION_A)
        real = startup_ack.process_presence

        def unreadable_first(proc_dir, pid, start):
            # A transient failure reading the first claude's /proc entry.
            return ChannelProcess.UNVERIFIABLE if pid == first.claude_pid else real(proc_dir, pid, start)
        with mock.patch.object(startup_ack, "process_presence", unreadable_first):
            # The next acknowledgement prunes the table first.
            self.acknowledged(SESSION_B)
        self.assertEqual(self.view()[SESSION_A]["refusal"], StartupRefusal.ALREADY_SENT.value)
        self.assertEqual(self.ack(SESSION_A, digest), (409, {"error": StartupRefusal.ALREADY_SENT.value}))
        self.assertEqual((first.typed(), len(self.tmux.sent)), (b"\r", 2))

    def test_a_full_table_of_live_attempts_refuses_rather_than_evicting(self):
        with mock.patch("llm_switchboard.desk.startup_ack.MAX_STARTUP_ATTEMPTS", 1):
            first, digest = self.acknowledged(SESSION_A)
            second = self.launch(SESSION_B)
            self.assertAckRefused(second, self.digest_for(second), StartupRefusal.ATTEMPTS_FULL, keys_issued=1)
            self.assertEqual(self.ack(SESSION_A, digest), (409, {"error": StartupRefusal.ALREADY_SENT.value}))
            # Once the first claude is proven gone (no /proc entry), its record goes.
            for name in ("stat", "cmdline", "environ", "exe"):
                (self.config.proc_dir / str(first.claude_pid) / name).unlink(missing_ok=True)
            (self.config.proc_dir / str(first.claude_pid)).rmdir()
            self.assertEqual(self.ack(SESSION_B, self.digest_for(second))[0], 200)
            wait_until(lambda: second.typed() == b"\r")


if __name__ == "__main__":
    unittest.main()
