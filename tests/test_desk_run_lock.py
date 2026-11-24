"""desk/run_lock.py: a process-lifetime exclusive flock and the
non-blocking probe the offline decisions take."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from llm_switchboard.desk.run_lock import RunLockHeld, RunLockUnusable, hold_for_process, probe

REPO = Path(__file__).resolve().parents[1]
ALLOWANCE = 10.0


class RunLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "run" / "hub.lock"

    def python(self, code: str) -> subprocess.Popen:
        """A child running `code` with `path` bound; it reports on stdout."""
        prelude = ("import gc, os, subprocess, sys, time\n"
                   "from pathlib import Path\n"
                   "from llm_switchboard.desk.run_lock import RunLockHeld, hold_for_process, probe\n"
                   f"path = Path({str(self.path)!r})\n")
        child = subprocess.Popen([sys.executable, "-c", prelude + textwrap.dedent(code)], cwd=REPO,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._end, child)
        return child

    @staticmethod
    def _end(child):
        if child.poll() is None:
            child.kill()
        child.wait()
        for stream in (child.stdin, child.stdout):
            stream.close()

    def holder(self) -> subprocess.Popen:
        # The HeldRunLock is dropped and collected at once: the lock must stay.
        child = self.python("""
            hold_for_process(path)
            gc.collect()
            print("held", flush=True)
            sys.stdin.readline()
        """)
        self.assertEqual(child.stdout.readline(), "held\n")
        return child

    def assert_free(self):
        with probe(self.path):
            pass

    def test_hold_lasts_the_process_and_probe_refuses_meanwhile(self):
        child = self.holder()
        for _ in range(3):
            with self.assertRaises(RunLockHeld), probe(self.path):
                self.fail("probe entered a held lock")
        child.stdin.write("\n")
        child.stdin.flush()
        self.assertEqual(child.wait(ALLOWANCE), 0)
        self.assert_free()

    def test_file_is_private_and_never_written(self):
        child = self.holder()
        info = os.stat(self.path)
        self.assertEqual((info.st_mode & 0o777, info.st_size), (0o600, 0))
        self.assertEqual(os.stat(self.path.parent).st_mode & 0o777, 0o700)
        child.kill()
        child.wait()
        self.assertTrue(self.path.exists())

    def test_probe_holds_for_its_block_only(self):
        with probe(self.path):
            child = self.python("""
                try:
                    with probe(path):
                        print("entered", flush=True)
                except RunLockHeld:
                    print("refused", flush=True)
            """)
            self.assertEqual(child.stdout.readline(), "refused\n")
        self.assert_free()

    def test_descriptor_not_inherited_by_children(self):
        # The holder starts a long-lived child with close_fds=False and exits:
        # with O_CLOEXEC the exec drops the descriptor, so the lock goes too.
        child = self.python("""
            hold_for_process(path)
            grandchild = subprocess.Popen(["/bin/sleep", "60"], close_fds=False)
            print(grandchild.pid, flush=True)
        """)
        grandchild = int(child.stdout.readline())
        self.addCleanup(lambda: _kill_quietly(grandchild))
        self.assertEqual(child.wait(ALLOWANCE), 0)
        os.kill(grandchild, 0)  # still running
        self.assert_free()

    def test_symlink_refused_and_its_target_untouched(self):
        self.path.parent.mkdir(mode=0o700)
        target = self.dir / "elsewhere"
        self.path.symlink_to(target)
        with self.assertRaises(RunLockUnusable), probe(self.path):
            pass
        with self.assertRaises(RunLockUnusable):
            hold_for_process(self.path)
        self.assertFalse(target.exists())

    def test_fifo_refused_promptly(self):
        self.path.parent.mkdir(mode=0o700)
        os.mkfifo(self.path, 0o600)
        started = time.monotonic()
        with self.assertRaises(RunLockUnusable), probe(self.path):
            pass
        self.assertLess(time.monotonic() - started, ALLOWANCE)

    def test_start_wait_is_bounded(self):
        child = self.holder()
        self.addCleanup(child.kill)
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with self.assertRaises(RunLockHeld):
            hold_for_process(self.path, wait_seconds=5, poll_seconds=0.5, clock=lambda: clock[0], sleep=sleep)
        self.assertEqual(sum(sleeps), 5)
        self.assertTrue(all(s == 0.5 for s in sleeps))

    def test_start_wait_takes_the_lock_once_released(self):
        child = self.holder()
        waiter = self.python("""
            try:
                hold_for_process(path, wait_seconds=60, poll_seconds=0.05)
                print("held", flush=True)
            except RunLockHeld:
                print("refused", flush=True)
            sys.stdin.readline()
        """)
        time.sleep(0.3)  # the waiter is polling a held lock
        child.stdin.write("\n")
        child.stdin.flush()
        self.assertEqual(waiter.stdout.readline(), "held\n")
        with self.assertRaises(RunLockHeld), probe(self.path):
            pass


def _kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


if __name__ == "__main__":
    unittest.main()
