"""desk/child_process.py: every child is reaped on every path, the caller's
timeout comes at the deadline, and the cleanup (SIGTERM, the grace, SIGKILL,
waitpid) is a separate bound after it."""

import ast
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

from llm_switchboard.desk import child_process
from llm_switchboard.desk.child_process import (
    MAX_PIPE_PAYLOAD_BYTES, TERM_GRACE_SECONDS, data_pipes, run_bounded, spawn,
)

ENV = {"PATH": "/usr/bin:/bin"}
# `exec` keeps one process: the ignored SIGTERM is inherited by sleep, and
# SIGKILL leaves no grandchild holding the pipes.
IGNORES_TERM = ["/bin/sh", "-c", "trap '' TERM; echo ready; exec sleep 60"]
# Generous for a loaded machine; every bound under test is far shorter
# than the child's own 60 s life, which is what a lost cleanup would show.
ALLOWANCE = 8.0


def assert_reaped(test, child):
    test.assertTrue(child.reaped.wait(TERM_GRACE_SECONDS + ALLOWANCE), "never reaped")
    with test.assertRaises(ChildProcessError):  # waitpid already done: no zombie
        os.waitpid(child.pid, os.WNOHANG)
    with test.assertRaises(ProcessLookupError):
        os.kill(child.pid, 0)


class ChildProcessTest(unittest.TestCase):
    def test_every_child_reaped_on_every_path(self):
        cases = {
            "normal exit": dict(argv=["/bin/echo", "hi"], deadline_seconds=ALLOWANCE),
            "deadline": dict(argv=["/bin/sleep", "60"], deadline_seconds=0.5),
            "stdout over its cap": dict(argv=["/bin/sh", "-c", "yes; sleep 60"], deadline_seconds=ALLOWANCE),
            "stdout closed, still running": dict(argv=["/bin/sh", "-c", "exec >&-; exec sleep 60"],
                                                 deadline_seconds=0.5),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                result = run_bounded(env=ENV, stdout_limit=1024, stderr_limit=1024, **kwargs)
                assert_reaped(self, result.child)
        with self.subTest("exception while reading"):
            spawned = []
            real_spawn = child_process.spawn

            def recording_spawn(*args, **kwargs):
                spawned.append(real_spawn(*args, **kwargs))
                return spawned[-1]
            with mock.patch.object(child_process, "spawn", side_effect=recording_spawn), \
                    mock.patch.object(child_process, "_exchange", side_effect=RuntimeError("boom")), \
                    self.assertRaises(RuntimeError):
                run_bounded(["/bin/sleep", "60"], env=ENV, deadline_seconds=ALLOWANCE,
                            stdout_limit=10, stderr_limit=10)
            [child] = spawned
            assert_reaped(self, child)

    def test_normal_exit_returns_status_and_output(self):
        result = run_bounded(["/bin/sh", "-c", "cat; echo err >&2; exit 3"], env=ENV, input=b"in\n",
                             deadline_seconds=ALLOWANCE, stdout_limit=100, stderr_limit=100)
        self.assertEqual((result.returncode, result.stdout, result.stderr, result.timed_out),
                         (3, b"in\n", b"err\n", False))

    def test_output_cut_at_each_cap(self):
        result = run_bounded(["/bin/sh", "-c", "yes out & yes err >&2"], env=ENV, deadline_seconds=ALLOWANCE,
                             stdout_limit=100, stderr_limit=50)
        self.assertEqual((len(result.stdout), len(result.stderr)), (100, 50))
        self.assertTrue(result.stdout_overflow and result.stderr_overflow)
        assert_reaped(self, result.child)

    def test_timeout_at_the_deadline_cleanup_after_it(self):
        # The child ignores SIGTERM, so the cleanup takes the whole grace:
        # the caller's timeout must come before it, not after.
        with mock.patch.object(child_process, "TERM_GRACE_SECONDS", 30):
            started = time.monotonic()
            result = run_bounded(IGNORES_TERM, env=ENV, deadline_seconds=1.0, stdout_limit=100, stderr_limit=100)
            elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertFalse(result.child.reaped.is_set(), "the caller waited for the cleanup")
        os.kill(result.child.pid, signal.SIGKILL)  # end the 30 s grace early
        assert_reaped(self, result.child)

    def test_child_ignoring_sigterm_killed_after_the_grace(self):
        child = spawn(IGNORES_TERM, env=ENV)
        self.assertEqual(child.popen.stdout.readline(), b"ready\n")  # the trap is set
        started = time.monotonic()
        child.cleanup()
        child.cleanup()  # idempotent
        assert_reaped(self, child)
        self.assertGreaterEqual(time.monotonic() - started, TERM_GRACE_SECONDS)
        self.assertEqual(child.popen.returncode, -signal.SIGKILL)

    def test_interpreter_waits_for_cleanup_started_by_daemon(self):
        script = textwrap.dedent(f"""\
            import threading
            from llm_switchboard.desk.child_process import run_bounded

            def run():
                result = run_bounded({IGNORES_TERM!r}, env={ENV!r}, deadline_seconds=1.0,
                                     stdout_limit=100, stderr_limit=100)
                print(result.child.pid, result.timed_out, result.stdout == b"ready\\n", flush=True)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            worker.join()
            # The caller is done; main exits without waiting on the child.
            """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                timeout=1.0 + TERM_GRACE_SECONDS + ALLOWANCE)
        self.assertEqual(result.returncode, 0, result.stderr)
        pid, timed_out, ready = result.stdout.split()
        pid = int(pid)
        try:
            with self.assertRaises(ProcessLookupError, msg="child still exists after interpreter shutdown"):
                os.kill(pid, 0)
        except AssertionError:
            # Don't leak the fixture child if this regression comes back.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        self.assertEqual((timed_out, ready), ("True", "True"))

    def test_child_that_obeys_sigterm_is_not_kept_for_the_grace(self):
        child = spawn(["/bin/sleep", "60"], env=ENV)
        child.cleanup()
        assert_reaped(self, child)
        self.assertEqual(child.popen.returncode, -signal.SIGTERM)

    def test_child_gets_only_the_given_env_and_descriptors(self):
        stray_read, stray_write = os.pipe()
        self.addCleanup(os.close, stray_read)
        self.addCleanup(os.close, stray_write)
        os.set_inheritable(stray_write, True)
        with data_pipes(b"payload") as (fd,):
            script = ("import os,sys; print(sorted(os.environ)); "
                      f"print(open('/dev/fd/{fd}','rb').read()); "
                      f"os.fstat({stray_write})")
            result = run_bounded([sys.executable, "-I", "-c", script], env=ENV, pass_fds=(fd,),
                                 deadline_seconds=ALLOWANCE, stdout_limit=4096, stderr_limit=4096)
        lines = result.stdout.decode().splitlines()
        # The interpreter itself may add a locale variable (PEP 538, macOS's CF encoding).
        self.assertEqual(set(ast.literal_eval(lines[0])) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}, {"PATH"})
        self.assertEqual(lines[1], "b'payload'")
        self.assertNotEqual(result.returncode, 0)  # the stray descriptor was not inherited
        self.assertIn(b"Bad file descriptor", result.stderr)

    def test_data_pipe_payload_capped(self):
        with self.assertRaises(ValueError), data_pipes(b"x" * (MAX_PIPE_PAYLOAD_BYTES + 1)):
            pass

    def test_unstartable_child_raises_oserror(self):
        with self.assertRaises(OSError):
            run_bounded(["/nonexistent/program"], env=ENV, deadline_seconds=1, stdout_limit=1, stderr_limit=1)


if __name__ == "__main__":
    unittest.main()
