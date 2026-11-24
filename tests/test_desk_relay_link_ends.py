"""How a relay link attempt ends (relay_link, docs/voice-desk.md,
"Deadlines, heartbeats and backpressure"): the 30 s handshake deadline and
the ssh child's cleanup, ssh's exits and their classification, the backoff
each end calls for, and `RelayConnector`. Python scripts stand in for ssh;
the real pump (`bin/desk-link`) and a test-side hub (`FakeHub`) for the
links that get up."""

import os
import signal
import threading
import time
import unittest

from desk_fixtures import wait_until
from link_peer import MACHINE
from relay_fixture import (
    EPOCH, GENEROUS, NONCE, FakeChannels, LinkCase, Run, fake_sign, pump_invocation, script_invocation,
)
from llm_switchboard.desk.link_protocol import (
    BACKOFF_MAX_SECONDS, FIXED_BACKOFF_SECONDS, HUB_NOT_RUNNING_BACKOFF_MAX_SECONDS, LinkType, RefusedCode, Side,
    encode_frame,
)
from llm_switchboard.desk.relay_link import Attempt, Backoff, LinkEnd, RelayConnector, fixed_backoff
from llm_switchboard.desk.relay_ssh import ConfigUnsupported, SshInvocation, SshStderr


SILENT = "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(120)"
PARTIAL = ("import sys, time; sys.stdout.write('{\"type\":\"link_chal'); sys.stdout.flush(); "
           "open(sys.argv[1], 'w').close(); time.sleep(120)")
CHALLENGE_ONLY = ("import sys, time; sys.stdin.readline(); sys.stdout.write(sys.argv[2]); sys.stdout.flush(); "
                  "sys.stdin.readline(); open(sys.argv[1], 'w').close(); time.sleep(120)")
DEAF = ("import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').close(); time.sleep(120)")


class ChildrenTest(LinkCase):
    def _timeout(self, script: str, *args: str) -> Run:
        marker = self.mac.home / f"ready-{len(os.listdir(self.mac.home))}"
        run = self.start(script_invocation(self.mac.home, script, str(marker), *args))
        wait_until(marker.exists, timeout=GENEROUS)
        self.clock.advance(29)
        time.sleep(0.6)  # two poll slices
        self.assertTrue(run.thread.is_alive(), "ended before the 30 s deadline")
        self.clock.advance(1)
        self.assertEqual(run.ended().code, LinkEnd.HANDSHAKE_TIMEOUT)
        self.assertEqual(Backoff(rng=lambda: 0.5).after(run.attempt), 1)
        self.assertTrue(run.link.child.reaped.wait(GENEROUS))
        with self.assertRaises(ChildProcessError):
            os.waitpid(run.link.child.pid, os.WNOHANG)
        return run

    def test_relay_handshake_deadline_reaps_ssh(self):
        challenge = encode_frame(LinkType.LINK_CHALLENGE, Side.HUB, version=1, epoch=EPOCH, nonce=NONCE).decode()
        for name, script, args in (("silent", SILENT, ()), ("partial line", PARTIAL, ()),
                                   ("no welcome", CHALLENGE_ONLY, (challenge,))):
            with self.subTest(name):
                run = self._timeout(script, *args)
                self.assertEqual(run.link.child.popen.returncode, -signal.SIGTERM)
        with self.subTest("ignores SIGTERM"):
            run = self._timeout(DEAF)
            # The caller's timeout comes at the deadline, before the cleanup ends.
            self.assertFalse(run.reaped_at_return)
            self.assertEqual(run.link.child.popen.returncode, -signal.SIGKILL)
            reaped_after = time.monotonic() - run.returned_at
            self.assertGreaterEqual(reaped_after, 1.5)  # SIGKILL only once the 2 s grace is over

    def test_ssh_child_reaped_on_every_path(self):
        # The relay's half of child_process's test_every_child_reaped_on_every_path:
        # every way an attempt ends leaves no ssh running and no zombie.
        class RaisingChannels(FakeChannels):
            def link_up(self, uplink):
                raise RuntimeError("boom")

        def raising_sign(message, deadline_seconds):
            raise RuntimeError("boom")

        def welcomed(run):
            self.hub.handshake()

        def challenged(run):
            self.hub.accept()
            self.hub.expect(LinkType.LINK_HELLO)
            self.hub.send(LinkType.LINK_CHALLENGE, version=1, epoch=EPOCH, nonce=NONCE)

        def retired(run):
            self.hub.handshake()
            self.channels.wait_for("up")
            run.link.retire("test")

        def hub_closed(run):
            self.hub.handshake()
            self.channels.wait_for("up")
            self.hub.sock.close()

        cases = [
            ("exception in the link", dict(channels=RaisingChannels()), welcomed, LinkEnd.RELAY_ERROR),
            ("exception while signing", dict(sign=raising_sign), challenged, LinkEnd.RELAY_ERROR),
            ("retired", {}, retired, LinkEnd.RETIRED),
            ("hub closed", {}, hub_closed, LinkEnd.LINK_CLOSED),
        ]
        for name, options, drive, code in cases:
            with self.subTest(name), self.assertLogs("llm_switchboard.desk.relay", "INFO"):
                self.channels = FakeChannels()
                run = self.start(**options)
                drive(run)
                self.assertEqual(run.ended().code, code)
                self.assertTrue(run.link.child.reaped.wait(GENEROUS))
                with self.assertRaises(ChildProcessError):
                    os.waitpid(run.link.child.pid, os.WNOHANG)
        with self.subTest("normal exit"):
            run = self.start(script_invocation(self.mac.home, "import sys; sys.exit(0)"))
            self.assertEqual(run.ended().code, LinkEnd.SSH_EXITED)
            self.assertTrue(run.link.child.reaped.wait(GENEROUS))
            with self.assertRaises(ChildProcessError):
                os.waitpid(run.link.child.pid, os.WNOHANG)

    def _exit(self, script: str) -> Attempt:
        run = self.start(script_invocation(self.mac.home, script))
        attempt = run.ended()
        self.assertTrue(run.link.child.reaped.wait(GENEROUS))
        return attempt

    def test_ssh_exits_classified(self):
        cases = [
            ("me@pi: Permission denied (publickey).", 255, LinkEnd.SSH_EXITED, SshStderr.PERMISSION_DENIED, True),
            ("Host key verification failed.", 255, LinkEnd.SSH_EXITED, SshStderr.HOST_KEY, True),
            ("connect to host pi port 22: Connection refused", 255, LinkEnd.SSH_EXITED,
             SshStderr.CONNECTION_REFUSED, False),
            ("python3: can't open file", 2, LinkEnd.FORCED_COMMAND_FAILED, SshStderr.OTHER, True),
            ("", 0, LinkEnd.SSH_EXITED, SshStderr.OTHER, False),
        ]
        for text, status, code, stderr, fixed in cases:
            with self.subTest(text=text, status=status):
                attempt = self._exit(f"import sys; sys.stderr.write({text!r}); sys.exit({status})")
                self.assertEqual((attempt.code, attempt.returncode, attempt.stderr), (code, status, stderr))
                self.assertEqual(fixed_backoff(attempt), fixed)

    def test_first_line_not_a_link_frame(self):
        attempt = self._exit("import sys, time; print('Welcome to pi'); sys.stdout.flush(); time.sleep(120)")
        self.assertEqual(attempt.code, LinkEnd.FORCED_COMMAND_FAILED)
        self.assertTrue(fixed_backoff(attempt))

    def test_ssh_unavailable(self):
        invocation = SshInvocation(("/nonexistent/ssh",), self.mac.home, self.mac.home / "k", "mac")
        run = self.start(invocation)
        self.assertEqual(self.assert_down(run, LinkEnd.SSH_UNAVAILABLE).code, LinkEnd.SSH_UNAVAILABLE)


class BackoffTest(unittest.TestCase):
    def test_doubles_to_60s_with_jitter(self):
        backoff = Backoff(rng=lambda: 0.5)
        delays = [backoff.after(Attempt(LinkEnd.HANDSHAKE_TIMEOUT)) for _ in range(8)]
        self.assertEqual(delays, [1, 2, 4, 8, 16, 32, BACKOFF_MAX_SECONDS, BACKOFF_MAX_SECONDS])
        self.assertAlmostEqual(Backoff(rng=lambda: 0.0).after(Attempt(LinkEnd.IDLE)), 0.8)
        self.assertAlmostEqual(Backoff(rng=lambda: 1.0).after(Attempt(LinkEnd.IDLE)), 1.2)

    def test_reset_once_a_link_stayed_up_60s(self):
        backoff = Backoff(rng=lambda: 0.5)
        for _ in range(4):
            backoff.after(Attempt(LinkEnd.LINK_CLOSED))
        self.assertEqual(backoff.after(Attempt(LinkEnd.LINK_CLOSED, up_seconds=59)), 16)
        self.assertEqual(backoff.after(Attempt(LinkEnd.LINK_CLOSED, up_seconds=60)), 1)

    def test_hub_not_running_doubles_to_5_minutes(self):
        backoff = Backoff(rng=lambda: 0.5)
        delays = [backoff.after(Attempt(RefusedCode.HUB_NOT_RUNNING)) for _ in range(11)]
        self.assertEqual(delays[-3:], [256, HUB_NOT_RUNNING_BACKOFF_MAX_SECONDS, HUB_NOT_RUNNING_BACKOFF_MAX_SECONDS])

    def test_fixed_ends_wait_10_minutes(self):
        fixed = [Attempt(code) for code in (RefusedCode.VERSION, RefusedCode.NOT_AUTHORIZED, RefusedCode.PROOF_FAILED,
                                            RefusedCode.VERIFIER_UNAVAILABLE, LinkEnd.SSH_CONFIG_UNSUPPORTED,
                                            LinkEnd.FORCED_COMMAND_FAILED)]
        fixed += [Attempt(LinkEnd.SSH_EXITED, 255, SshStderr.PERMISSION_DENIED),
                  Attempt(LinkEnd.SSH_EXITED, 255, SshStderr.HOST_KEY)]
        for attempt in fixed:
            self.assertEqual(Backoff(rng=lambda: 0.9).after(attempt), FIXED_BACKOFF_SECONDS, attempt)
        normal = [Attempt(code) for code in (RefusedCode.ADMISSION_TIMEOUT, RefusedCode.TOO_MANY_RECONNECTS,
                                             RefusedCode.SIGNERS_UNREADABLE, LinkEnd.STALE)]
        normal += [Attempt(LinkEnd.SSH_EXITED, 255, SshStderr.TIMEOUT),
                   Attempt(LinkEnd.SSH_EXITED, 1, SshStderr.PERMISSION_DENIED)]
        for attempt in normal:
            self.assertEqual(Backoff(rng=lambda: 0.5).after(attempt), 1, attempt)



class _StopAfter(Backoff):
    def __init__(self, connector_ref: list, attempts: int):
        super().__init__(rng=lambda: 0.5)
        self.connector_ref = connector_ref
        self.attempts: list[Attempt] = []
        self.limit = attempts

    def after(self, attempt):
        self.attempts.append(attempt)
        if len(self.attempts) >= self.limit:
            self.connector_ref[0].stop()
        return 0


class ConnectorTest(LinkCase):
    def connector(self, prepare, attempts: int, **options) -> tuple[RelayConnector, _StopAfter]:
        ref = []
        backoff = _StopAfter(ref, attempts)
        options.setdefault("sign", fake_sign)
        options.setdefault("reader", self.mac.kernel)
        connector = RelayConnector(self.mac.config, self.channels, machine=MACHINE, prepare=prepare, backoff=backoff,
                                   **options)
        ref.append(connector)
        return connector, backoff

    def run_connector(self, connector) -> None:
        thread = threading.Thread(target=connector.run, daemon=True)
        thread.start()
        self.addCleanup(connector.stop)
        return thread

    def test_unsupported_config_waits_fixed_and_logs_once(self):
        def prepare(config):
            raise ConfigUnsupported()

        connector, backoff = self.connector(prepare, 3)
        with self.assertLogs("llm_switchboard.desk.relay", "INFO") as logs:
            connector.run()
        self.assertEqual([a.code for a in backoff.attempts], [LinkEnd.SSH_CONFIG_UNSUPPORTED] * 3)
        self.assertEqual(sum("ssh_config_unsupported" in line for line in logs.output), 1)

    def test_fresh_route_each_attempt_and_stderr_never_logged(self):
        calls = []
        script = "import sys; sys.stderr.write('Permission denied MARKER-7Q'); sys.exit(255)"

        def prepare(config):
            calls.append(config)
            return script_invocation(self.mac.home, script)

        connector, backoff = self.connector(prepare, 3)
        with self.assertLogs("llm_switchboard.desk.relay", "INFO") as logs:
            connector.run()
        self.assertEqual(len(calls), 3)
        self.assertEqual([(a.code, a.stderr) for a in backoff.attempts],
                         [(LinkEnd.SSH_EXITED, SshStderr.PERMISSION_DENIED)] * 3)
        self.assertEqual(sum("permission_denied" in line for line in logs.output), 1)
        self.assertFalse(any("MARKER" in line for line in logs.output))

    def test_stop_retires_the_current_link(self):
        connector, backoff = self.connector(lambda config: pump_invocation(self.mac.home), 99, clock=self.clock)
        thread = self.run_connector(connector)
        self.hub.handshake()
        link = self.channels.wait_for("up")[0][1]
        connector.stop()
        thread.join(GENEROUS)
        self.assertFalse(thread.is_alive())
        self.assertTrue(link.retired)
        self.assertEqual(backoff.attempts[-1].code, LinkEnd.RETIRED)
        self.assertEqual(self.channels.calls[-1][:2], ("down", link))


if __name__ == "__main__":
    unittest.main()
