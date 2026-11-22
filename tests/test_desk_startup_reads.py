"""The startup acknowledgement's reads of `enrolled.json`: opened and
validated on every acknowledgement and listing, never answered from the
routing cache, so a permission change is seen at once in both directions."""

import os
import unittest

from desk_fixtures import SESSION_A
from test_desk_startup_ack import StartupCase

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


if __name__ == "__main__":
    unittest.main()
