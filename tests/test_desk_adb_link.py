"""bin/desk-adb-link against a fake adb that records its argv and answers from files."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "desk-adb-link"

FAKE_ADB = """#!/bin/bash
# Fake adb: logs argv; answers from files in $FAKE_DIR. wait-for-device fails on
# its 3rd call so the script's loop ends.
echo "$*" >> "$FAKE_DIR/log"
shift 2   # -s <serial>
case "$1" in
  wait-for-device)
    n=$(( $(cat "$FAKE_DIR/waits" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$FAKE_DIR/waits"
    [ "$n" -lt 3 ] || exit 1 ;;
  get-state) cat "$FAKE_DIR/state" ;;
  reverse)
    if [ "$2" = "--list" ]; then cat "$FAKE_DIR/reverse_list" 2>/dev/null; exit 0; fi
    exit "$(cat "$FAKE_DIR/reverse_rc" 2>/dev/null || echo 0)" ;;
  wait-for-disconnect) exit 0 ;;
  *) exit 99 ;;
esac
"""

FORBIDDEN = ("kill-server", "start-server", "shell", "reverse --remove", " am ", "input")


class DeskAdbLinkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        adb = bin_dir / "adb"
        adb.write_text(FAKE_ADB)
        adb.chmod(0o755)
        (self.dir / "state").write_text("device\n")
        self.env = dict(os.environ, FAKE_DIR=str(self.dir), PATH=f"{bin_dir}:{os.environ['PATH']}")

    def run_link(self, serial="TAB123", port="8796"):
        return subprocess.run([str(SCRIPT), serial, port], env=self.env,
                              capture_output=True, text=True, timeout=30, check=False)

    def calls(self):
        log = self.dir / "log"
        return log.read_text().splitlines() if log.exists() else []

    def assert_no_forbidden_calls(self):
        for line in self.calls():
            line = f" {line} "
            for word in FORBIDDEN:
                self.assertNotIn(word, line)

    def test_unauthorized_device_exits_3(self):
        (self.dir / "state").write_text("unauthorized\n")
        result = self.run_link()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual([c for c in self.calls() if "reverse" in c], [])
        self.assert_no_forbidden_calls()

    def test_offline_device_exits_3(self):
        (self.dir / "state").write_text("offline\n")
        self.assertEqual(self.run_link().returncode, 3)

    def test_identical_mapping_is_adopted_without_reverse_call(self):
        (self.dir / "reverse_list").write_text("UsbFfs tcp:8796 tcp:8796\n")
        result = self.run_link()
        self.assertEqual(result.returncode, 1)  # the fake's 3rd wait-for-device ends the loop
        self.assertIn("adopted", result.stderr)
        self.assertEqual([c for c in self.calls() if "--no-rebind" in c], [])
        self.assertIn("-s TAB123 wait-for-disconnect", self.calls())
        self.assert_no_forbidden_calls()

    def test_reverse_marker_format_is_parsed(self):
        (self.dir / "reverse_list").write_text("(reverse) tcp:8796 tcp:8796\n")
        result = self.run_link()
        self.assertIn("adopted", result.stderr)
        self.assertEqual([c for c in self.calls() if "--no-rebind" in c], [])

    def test_different_mapping_is_refused_exit_4(self):
        (self.dir / "reverse_list").write_text("TAB123 tcp:8796 tcp:9999\n")
        result = self.run_link()
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual([c for c in self.calls() if "--no-rebind" in c], [])
        self.assertNotIn("wait-for-disconnect", " ".join(self.calls()))

    def test_other_ports_mappings_are_ignored_and_left_alone(self):
        (self.dir / "reverse_list").write_text("TAB123 tcp:9000 tcp:9999\n")
        result = self.run_link()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("-s TAB123 reverse --no-rebind tcp:8796 tcp:8796", self.calls())
        self.assert_no_forbidden_calls()

    def test_fresh_mapping_is_created_with_no_rebind_and_rechecked_after_reconnect(self):
        result = self.run_link()
        self.assertEqual(result.returncode, 1, result.stderr)
        calls = self.calls()
        self.assertEqual(calls.count("-s TAB123 reverse --no-rebind tcp:8796 tcp:8796"), 2)
        self.assertEqual(calls.count("-s TAB123 wait-for-disconnect"), 2)
        self.assertEqual(calls.count("-s TAB123 reverse --list"), 2)
        for call in calls:
            self.assertTrue(call.startswith("-s TAB123 "), call)
        self.assert_no_forbidden_calls()

    def test_reverse_failure_exits_5(self):
        (self.dir / "reverse_rc").write_text("1\n")
        self.assertEqual(self.run_link().returncode, 5)

    def test_bad_arguments_are_rejected_before_adb_runs(self):
        for serial, port in (("bad serial", "8796"), ("a;b", "8796"), ("", "8796"),
                             ("TAB123", "80"), ("TAB123", "70000"), ("TAB123", "abc"), ("TAB123", "")):
            with self.subTest(serial=serial, port=port):
                self.assertEqual(self.run_link(serial, port).returncode, 2)
        self.assertEqual(subprocess.run([str(SCRIPT)], env=self.env, capture_output=True).returncode, 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
