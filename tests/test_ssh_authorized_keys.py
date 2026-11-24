"""The marked-line edit of authorized_keys that host-authorize (and the
desk link's commands) share: only a line whose final comment is the marker
is replaced or removed, every other line is kept byte for byte."""

from pathlib import Path
import shutil
import tempfile
import unittest

from llm_switchboard import ssh_authorized_keys as keys

MARKER = b"llm-switchboard-desk-link:mac"
OTHERS = (b"# a comment\r\n"
          b"ssh-ed25519 AAAA1 someone llm-switchboard-desk-link:mac-mini\n"
          b"ssh-ed25519 AAAA2 llm-switchboard-desk-link:mac in the middle\n"
          b"ssh-ed25519 AAAA3 no newline at the end")


class MarkedLineTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.path = keys.authorized_keys_path(self.home)

    def test_replace_keeps_every_other_line_and_is_idempotent(self):
        self.path.write_bytes(OTHERS)
        line = b"restrict ssh-ed25519 AAAA9 " + MARKER + b"\n"
        with keys.locked(self.path):
            self.assertTrue(keys.replace_marked_line(self.path, MARKER, line))
        self.assertEqual(self.path.read_bytes(), OTHERS + b"\n" + line)
        with keys.locked(self.path):
            self.assertFalse(keys.replace_marked_line(self.path, MARKER, line))
            newer = b"restrict ssh-ed25519 AAAA8 " + MARKER + b"\n"
            self.assertTrue(keys.replace_marked_line(self.path, MARKER, newer))
        self.assertEqual(self.path.read_bytes(), OTHERS + b"\n" + newer)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_remove_drops_only_the_marked_line(self):
        line = b"restrict ssh-ed25519 AAAA9 " + MARKER + b"\r\n"
        self.path.write_bytes(line + OTHERS)
        with keys.locked(self.path):
            self.assertTrue(keys.remove_marked_line(self.path, MARKER))
            self.assertFalse(keys.remove_marked_line(self.path, MARKER))
        self.assertEqual(self.path.read_bytes(), OTHERS + b"\n")

    def test_removing_from_a_missing_file_creates_nothing(self):
        with keys.locked(self.path):
            self.assertFalse(keys.remove_marked_line(self.path, MARKER))
        self.assertFalse(self.path.exists())

    def test_new_line_must_carry_its_marker(self):
        with keys.locked(self.path), self.assertRaises(ValueError):
            keys.replace_marked_line(self.path, MARKER, b"ssh-ed25519 AAAA9 other\n")
        self.assertFalse(self.path.exists())

    def test_symlinked_file_refused(self):
        self.path.symlink_to(self.home / "elsewhere")
        with self.assertRaises(ValueError):
            keys.authorized_keys_path(self.home)


if __name__ == "__main__":
    unittest.main()
