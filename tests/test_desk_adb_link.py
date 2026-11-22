"""bin/desk-adb-link against a fake adb that records its argv and answers from queues.

The fake answers `adb devices` from a queue of states (absent, device,
unauthorized, offline), one per call, repeating the last one once the queue is
spent and touching an `exhausted` marker so the test can stop the otherwise
endless loop. get-state and wait-for-device exit 99: the script must not use them.
"""

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "desk-adb-link"

FAKE_ADB = """#!/bin/bash
echo "$*" >> "$FAKE_DIR/log"
pop() {  # pop <queue file> <default>
  local file=$1 value
  value=$(head -n 1 "$file" 2>/dev/null || true)
  if [ -n "$value" ]; then
    tail -n +2 "$file" > "$file.tmp"; mv "$file.tmp" "$file"
    echo "$value" > "$file.last"
  else
    touch "$FAKE_DIR/exhausted"
    value=$(cat "$file.last" 2>/dev/null || echo "$2")
  fi
  echo "$value"
}
if [ "$1" = devices ]; then
  state=$(pop "$FAKE_DIR/states" absent)
  echo "List of devices attached"
  echo "OTHER999	device"
  [ "$state" = absent ] || printf 'TAB123\\t%s\\n' "$state"
  exit 0
fi
[ "$1" = -s ] || exit 99
shift 2   # -s <serial>
case "$1" in
  reverse)
    if [ "$2" = "--list" ]; then cat "$FAKE_DIR/reverse_list" 2>/dev/null; exit 0; fi
    exit "$(pop "$FAKE_DIR/reverse_rcs" 0)" ;;
  wait-for-disconnect) exit 0 ;;
  *) exit 99 ;;
esac
"""

FORBIDDEN = ("kill-server", "start-server", "shell", "reverse --remove", " am ", "input",
             "get-state", "wait-for-device")
CREATE = "-s TAB123 reverse --no-rebind tcp:8796 tcp:8796"


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
        self.env = dict(os.environ, FAKE_DIR=str(self.dir), DESK_ADB_POLL_SECONDS="0.02",
                        PATH=f"{bin_dir}:{os.environ['PATH']}")

    def script_states(self, *states, reverse_rcs=()):
        (self.dir / "states").write_text("".join(f"{s}\n" for s in states))
        (self.dir / "reverse_rcs").write_text("".join(f"{rc}\n" for rc in reverse_rcs))

    def run_link(self, serial="TAB123", port="8796"):
        """Runs until the script exits or the fake's queues run dry, then stops it.

        Returns (returncode or None if it was still looping, stderr)."""
        proc = subprocess.Popen([str(SCRIPT), serial, port], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)  # one group, so nothing outlives the temp dir
        deadline = time.monotonic() + 20
        while proc.poll() is None and time.monotonic() < deadline:
            if (self.dir / "exhausted").exists():
                time.sleep(0.15)  # let it complete a pass on the repeated last state
                break
            time.sleep(0.02)
        code = proc.poll()
        if code is None:
            os.killpg(proc.pid, signal.SIGTERM)
        _, stderr = proc.communicate(timeout=10)
        self.assertLess(time.monotonic(), deadline, "link loop never settled")
        return code, stderr

    def calls(self):
        log = self.dir / "log"
        return log.read_text().splitlines() if log.exists() else []

    def assert_no_forbidden_calls(self):
        for line in self.calls():
            line = f" {line} "
            for word in FORBIDDEN:
                self.assertNotIn(word, line)

    def test_state_transitions_wait_quietly_then_link_and_relink(self):
        # absent -> unauthorized -> offline -> device -> (gone) absent -> device
        self.script_states("absent", "absent", "unauthorized", "unauthorized", "offline",
                           "device", "absent", "device")
        code, err = self.run_link()
        self.assertIsNone(code, err)
        self.assertEqual(err.count("waiting for device"), 2, err)
        self.assertEqual(err.count("unauthorized"), 1, err)  # logged once per transition
        self.assertEqual(err.count("state is 'offline'"), 1, err)
        self.assertGreaterEqual(err.count("created tcp:8796 mapping"), 2, err)
        self.assertGreaterEqual(self.calls().count(CREATE), 2)
        self.assert_no_forbidden_calls()

    def test_unauthorized_device_never_exits_and_never_reverses(self):
        self.script_states("unauthorized", "unauthorized", "unauthorized")
        code, err = self.run_link()
        self.assertIsNone(code, err)
        self.assertEqual(err.count("unauthorized"), 1, err)
        self.assertEqual([c for c in self.calls() if "reverse" in c], [])
        self.assert_no_forbidden_calls()

    def test_offline_device_waits_without_exit(self):
        self.script_states("offline", "offline")
        code, err = self.run_link()
        self.assertIsNone(code, err)
        self.assertEqual([c for c in self.calls() if "reverse" in c], [])

    def test_unplug_between_check_and_reverse_is_a_disconnect_not_a_failure(self):
        # state device, reverse fails, device now absent, then it returns and links.
        self.script_states("device", "absent", "device", reverse_rcs=(1, 0))
        code, err = self.run_link()
        self.assertIsNone(code, err)
        self.assertIn("disconnected during setup", err)
        self.assertGreaterEqual(self.calls().count(CREATE), 2)
        self.assertIn("created tcp:8796 mapping", err)
        self.assert_no_forbidden_calls()

    def test_reverse_failure_with_device_present_is_retried_not_fatal(self):
        self.script_states("device", "device", "device", reverse_rcs=(1, 0))
        code, err = self.run_link()
        self.assertIsNone(code, err)
        self.assertIn("retrying", err)
        self.assertIn("created tcp:8796 mapping", err)

    def test_identical_mapping_is_adopted_without_reverse_call(self):
        self.script_states("device")
        (self.dir / "reverse_list").write_text("UsbFfs tcp:8796 tcp:8796\n")
        code, err = self.run_link()
        self.assertIsNone(code, err)
        self.assertIn("adopted", err)
        self.assertEqual([c for c in self.calls() if "--no-rebind" in c], [])
        self.assertIn("-s TAB123 wait-for-disconnect", self.calls())
        self.assert_no_forbidden_calls()

    def test_reverse_marker_format_is_parsed(self):
        self.script_states("device")
        (self.dir / "reverse_list").write_text("(reverse) tcp:8796 tcp:8796\n")
        code, err = self.run_link()
        self.assertIn("adopted", err)
        self.assertEqual([c for c in self.calls() if "--no-rebind" in c], [])

    def test_different_mapping_is_refused_exit_4(self):
        self.script_states("device")
        (self.dir / "reverse_list").write_text("TAB123 tcp:8796 tcp:9999\n")
        code, err = self.run_link()
        self.assertEqual(code, 4, err)
        self.assertEqual([c for c in self.calls() if "--no-rebind" in c], [])
        self.assertNotIn("wait-for-disconnect", " ".join(self.calls()))

    def test_other_ports_mappings_are_ignored_and_left_alone(self):
        self.script_states("device")
        (self.dir / "reverse_list").write_text("TAB123 tcp:9000 tcp:9999\n")
        code, err = self.run_link()
        self.assertIsNone(code, err)
        self.assertIn(CREATE, self.calls())
        self.assert_no_forbidden_calls()

    def test_fresh_mapping_is_rechecked_after_reconnect(self):
        self.script_states("device", "absent", "device")
        code, err = self.run_link()
        self.assertIsNone(code, err)
        calls = self.calls()
        self.assertGreaterEqual(calls.count(CREATE), 2)
        self.assertGreaterEqual(calls.count("-s TAB123 wait-for-disconnect"), 2)
        for call in calls:
            self.assertTrue(call.startswith(("-s TAB123 ", "devices")), call)
        self.assert_no_forbidden_calls()

    def test_only_the_named_serial_is_considered(self):
        self.script_states("absent", "absent")  # fake lists OTHER999 as a device always
        code, err = self.run_link()
        self.assertIsNone(code, err)
        self.assertEqual([c for c in self.calls() if c != "devices"], [])

    def test_bad_arguments_are_rejected_before_adb_runs(self):
        for serial, port in (("bad serial", "8796"), ("a;b", "8796"), ("", "8796"),
                             ("TAB123", "80"), ("TAB123", "70000"), ("TAB123", "abc"), ("TAB123", "")):
            with self.subTest(serial=serial, port=port):
                self.assertEqual(self.run_link(serial, port)[0], 2)
        self.assertEqual(subprocess.run([str(SCRIPT)], env=self.env, capture_output=True).returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_unit_stops_for_good_only_on_exit_2_and_4(self):
        unit = (REPO / "systemd" / "llm-switchboard-desk-adb.service").read_text()
        self.assertIn("RestartPreventExitStatus=2 4\n", unit)


if __name__ == "__main__":
    unittest.main()
