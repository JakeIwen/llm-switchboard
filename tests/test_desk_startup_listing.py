"""The startup listing never reports a failed read as absence: a pane whose
screen tmux couldn't read is listed as unavailable, an unreadable
enrolled.json fails the call, and a process entry that can't be read is
"can't be checked", never "no process" or one claude fewer."""

import os
import unittest

from desk_fixtures import SESSION_A
from test_desk_startup_ack import StartupCase

from llm_switchboard.desk.startup_identity import StartupRefusal


class ListingTest(StartupCase):
    def test_a_failed_capture_is_listed_unavailable(self):
        launch = self.launch()
        # The listing's capture and the inspection's both fail.
        self.tmux.failing_captures = 2
        item = self.view()[SESSION_A]
        self.assertEqual((item["capture"], item["digest"], item["refusal"]),
                         (None, None, StartupRefusal.TMUX_FAILED.value))
        # Only the listing's capture fails: the inspection reads the screen.
        self.tmux.failing_captures = 1
        self.assertIsNotNone(self.view()[SESSION_A]["digest"])
        # A pane that closed before its capture is gone, not unavailable.
        self.tmux.before_capture = [lambda: self.tmux.kill(launch.window_id)]
        self.assertEqual(self.view(), {})

    def test_an_unreadable_enrollment_fails_the_listing(self):
        self.launch()
        self.config.enrolled_file.write_text("{not json")
        status, _, body = self.call("GET", "/desk/api/startup", token=self.token)
        self.assertEqual((status, body), (503, {"error": "enrolled.json unreadable"}))

    def test_an_unreadable_process_entry_cannot_be_checked(self):
        launch = self.launch()
        # Another process the scan can't read might be a second claude in the pane.
        self.write_process(4_100_001, launch.pane_pid, "800", ("claude",), pgrp=4_100_001, tpgid=launch.claude_pid)
        stat = self.config.proc_dir / "4100001" / "stat"
        # Mode 0 stops reads, not the home's removal (its folder stays writable).
        os.chmod(stat, 0)
        self.assertEqual(self.view()[SESSION_A]["refusal"], StartupRefusal.UNVERIFIABLE.value)
        self.assertAckRefused(launch, "0" * 64, StartupRefusal.UNVERIFIABLE)
        os.chmod(stat, 0o644)
        self.clock.advance(61)
        self.assertEqual(self.view()[SESSION_A]["refusal"], StartupRefusal.AMBIGUOUS_PROCESS.value)
        os.unlink(stat)
        # Exited mid-scan (no stat any more): skipped, not unreadable.
        self.assertIsNone(self.view()[SESSION_A]["refusal"])
        # The pane's own process unreadable: unknown, not "no foreground process".
        pane_stat = self.config.proc_dir / str(launch.pane_pid) / "stat"
        os.chmod(pane_stat, 0)
        self.assertEqual(self.view()[SESSION_A]["refusal"], StartupRefusal.UNVERIFIABLE.value)


if __name__ == "__main__":
    unittest.main()
