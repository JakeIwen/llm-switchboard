"""The startup acknowledgement's reads of `enrolled.json`: opened and
validated on every acknowledgement and listing, never answered from the
routing cache, so a permission change is seen at once in both directions;
and bounded, so a FIFO or an oversize file answers 503 at once. And the
scan for the pane's children: parentage first, from `stat` alone, so only
an actual child (or an entry whose parent can't be read) can refuse."""

import json
import os
import time
import unittest

from desk_fixtures import SESSION_A
from test_desk_startup_ack import StartupCase

from llm_switchboard.desk.model import MAX_DESK_FILE_BYTES
from llm_switchboard.desk.startup_identity import StartupRefusal

UNREADABLE = (503, {"error": "enrolled.json unreadable"})
_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@unittest.skipIf(_AS_ROOT, "root reads a file whatever its mode")
class EnrollmentPermissionTest(StartupCase):
    def lock_enrollment(self, mode):
        os.chmod(self.config.enrolled_file, mode)
        self.addCleanup(os.chmod, self.config.enrolled_file, 0o600)

    def test_an_unreadable_file_refuses_after_the_cache_was_warmed(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        # Routing's cached read has the enrollment; the mode change alters
        # none of the mtime, size and inode that cache is keyed on.
        self.assertIn(SESSION_A, self.hub.enrollment.sessions())
        self.lock_enrollment(0)
        self.assertEqual(self.ack(SESSION_A, digest), UNREADABLE)
        self.assertEqual(launch.typed(), b"")
        self.assertEqual(self.tmux.sent, [])
        status, _, body = self.call("GET", "/desk/api/startup", token=self.token)
        self.assertEqual((status, body), UNREADABLE)

    def test_a_restored_permission_works_again_without_a_content_change(self):
        launch = self.launch()
        self.lock_enrollment(0)
        status, _, body = self.call("GET", "/desk/api/startup", token=self.token)
        self.assertEqual((status, body), UNREADABLE)
        self.assertEqual(self.ack(SESSION_A, "0" * 64), UNREADABLE)
        os.chmod(self.config.enrolled_file, 0o600)
        digest = self.digest_for(launch)
        self.assertEqual(self.ack(SESSION_A, digest), (200, {"session_id": SESSION_A, "sent": True}))
        self.assertOneGuardedEnter(launch)


class EnrollmentFileShapeTest(StartupCase):
    def test_a_fifo_or_oversize_enrollment_answers_503_at_once(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        path = self.config.enrolled_file
        # Valid content, padded past the cap with JSON whitespace.
        oversize = json.dumps(json.loads(path.read_text())) + " " * MAX_DESK_FILE_BYTES
        for name, replace_file in (("FIFO with no writer", lambda: os.mkfifo(path)),
                                   ("oversize", lambda: path.write_text(oversize))):
            with self.subTest(name):
                path.unlink()
                replace_file()
                started = time.monotonic()
                # The client's own timeout bounds a hub that is stuck.
                status, _, body = self.call("POST", "/desk/api/startup/ack",
                                            {"session_id": SESSION_A, "digest": digest}, token=self.token, timeout=5)
                self.assertEqual((status, body), UNREADABLE)
                status, _, body = self.call("GET", "/desk/api/startup", token=self.token, timeout=5)
                self.assertEqual((status, body), UNREADABLE)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(launch.typed(), b"")
                self.assertEqual(self.tmux.sent, [])


class ChildScanTest(StartupCase):
    UNRELATED = 4_200_001

    def unrelated(self, name: bytes = b"x"):
        """A process outside the pane (its parent is init), named `name`."""
        self.write_process(self.UNRELATED, 1, "800", ("other",), pgrp=self.UNRELATED, tpgid=-1)
        stat = self.config.proc_dir / str(self.UNRELATED) / "stat"
        stat.write_bytes(stat.read_bytes().replace(b"(x)", b"(" + name + b")", 1))
        return self.config.proc_dir / str(self.UNRELATED)

    def assertAcknowledged(self, launch):
        self.assertEqual(self.ack(SESSION_A, self.digest_for(launch)), (200, {"session_id": SESSION_A, "sent": True}))
        self.assertOneGuardedEnter(launch)

    @unittest.skipIf(_AS_ROOT, "root reads a file whatever its mode")
    def test_an_unrelated_process_with_an_unreadable_command_line_does_not_block(self):
        launch = self.launch()
        os.chmod(self.unrelated() / "cmdline", 0)
        self.assertAcknowledged(launch)

    def test_an_unrelated_process_with_a_name_that_is_not_utf8_does_not_block(self):
        launch = self.launch()
        # Spaces, ")" and bytes that aren't UTF-8: the fields start after the last ")".
        self.unrelated(b"\xff\xfe) 9 (z")
        self.assertAcknowledged(launch)

    @unittest.skipIf(_AS_ROOT, "root reads a file whatever its mode")
    def test_an_existing_entry_whose_parent_cannot_be_read_still_refuses(self):
        launch = self.launch()
        os.chmod(self.unrelated() / "stat", 0)
        self.assertEqual(self.view()[SESSION_A]["refusal"], StartupRefusal.UNVERIFIABLE.value)
        self.assertAckRefused(launch, "0" * 64, StartupRefusal.UNVERIFIABLE)

    @unittest.skipIf(_AS_ROOT, "root reads a file whatever its mode")
    def test_a_child_whose_command_line_cannot_be_read_still_refuses(self):
        launch = self.launch()
        os.chmod(self.config.proc_dir / str(launch.claude_pid) / "cmdline", 0)
        self.assertEqual(self.view()[SESSION_A]["refusal"], StartupRefusal.UNVERIFIABLE.value)
        self.assertAckRefused(launch, "0" * 64, StartupRefusal.UNVERIFIABLE)


if __name__ == "__main__":
    unittest.main()
