"""The relay's launchd job (docs/voice-desk.md, "Processes, sockets and
files"; "Switching it off, revocation and rollback"; Tests, "Revocation and
rollback": `relay-install --unload`): the plist, install's checks, and the
unload's confirmation.

No test here calls launchctl or writes under the real ~/Library: every
launchd call goes to `FakeLaunchctl` through `relay_launchd.Launchd`, whose
LaunchAgents directory is a temporary one, and `subprocess.run` is patched
to fail the test if anything reaches it. A relay that keeps `relay.lock` is a
real child process holding it through `run_lock`, as the relay does.
"""

import argparse
import contextlib
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import unittest
from unittest import mock

from desk_fixtures import Clock, make_home
from llm_switchboard import host_launchd
from llm_switchboard.desk import relay_launchd
from llm_switchboard.desk.config import DeskConfig
from llm_switchboard.desk.relay_cli import relay_install
from llm_switchboard.local_config import MAC_NAME

REPO = Path(__file__).resolve().parents[1]
DOMAIN = "gui/501"
SERVICE = f"{DOMAIN}/{relay_launchd.LABEL}"
NAMES = {MAC_NAME: "mac"}
HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from llm_switchboard.desk.run_lock import hold_for_process\n"
    "hold_for_process(Path(sys.argv[2]))\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


class FakeLaunchctl:
    """launchctl for one user domain: `print` finds a loaded label (and,
    for `print_lag` calls after a bootout, one launchd is still removing);
    a label not there prints 113, as launchctl does."""

    def __init__(self, *, loaded=False, print_lag=0, print_status=None):
        self.calls: list[list[str]] = []
        self.loaded = loaded
        self.print_lag = print_lag
        self.print_status = print_status
        self._lag = 0

    def __call__(self, argv, check=False, capture_output=False):
        argv = [str(word) for word in argv]
        self.calls.append(argv)
        verb = argv[1]
        if verb == "bootout":
            status, self.loaded, self._lag = (0 if self.loaded else 3), False, self.print_lag
        elif verb == "bootstrap":
            status, self.loaded = 0, True
        elif verb == "print":
            if self.print_status is not None:
                status = self.print_status
            elif self.loaded or self._lag:
                self._lag = max(0, self._lag - 1)
                status = 0
            else:
                status = relay_launchd.SERVICE_NOT_FOUND
        else:
            raise AssertionError(f"unexpected launchctl verb {verb}")
        return subprocess.CompletedProcess(argv, status, b"", b"")


def _refuse_real_run(*args, **kwargs):
    raise AssertionError(f"a test reached subprocess.run: {args!r}")


class RelayLaunchdCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("subprocess.run", _refuse_real_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = make_home(self)
        self.config = DeskConfig.from_env(home=self.home, environ={})
        self.agents = self.home / "agents"
        self.fake = FakeLaunchctl()
        self.launchd = relay_launchd.Launchd(self.agents, DOMAIN, self.fake)
        self.clock = Clock()
        self.sleeps = 0
        self.on_sleep = None

    def add_key(self):
        ssh = self.home / ".ssh"
        ssh.mkdir(mode=0o700, exist_ok=True)
        (ssh / "llm-switchboard-desk-mac").write_text("private\n")

    def sleep(self, seconds):
        self.sleeps += 1
        self.clock.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep()

    def cli(self, *, unload, platform="darwin", environ=NAMES, config=None, wait_seconds=None):
        timing = {"clock": self.clock, "sleep": self.sleep}
        if wait_seconds is not None:
            timing["wait_seconds"] = wait_seconds
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = relay_install(argparse.Namespace(unload=unload), config or self.config, launchd=self.launchd,
                                 platform=platform, environ=environ, timing=timing)
        return code, out.getvalue(), err.getvalue()

    def hold_relay_lock(self) -> subprocess.Popen:
        """A process holding relay.lock the way a running relay does."""
        child = subprocess.Popen([sys.executable, "-I", "-c", HOLDER, str(REPO), str(self.config.relay_lock)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._end, child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    @staticmethod
    def _end(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)
        for stream in (child.stdin, child.stdout):
            stream.close()

    def install_loaded(self):
        self.agents.mkdir()
        self.launchd.plist.write_bytes(plistlib.dumps(relay_launchd.job(self.config)))
        self.fake.loaded = True


class RelayJobTest(RelayLaunchdCase):
    def test_relay_job_runs_the_checkout_relay(self):
        """The rendered launchd target exists: the checkout's interpreter and
        bin/desk-relay, the same argv DeskConfig pins."""
        config = DeskConfig.from_env(home=Path("/Users/me"), environ={})
        spec = relay_launchd.job(config)
        python, *flags, script = spec["ProgramArguments"]
        self.assertEqual(spec["ProgramArguments"], list(config.relay_argv))
        self.assertEqual((python, flags, script),
                         (str(REPO / ".venv" / "bin" / "python"), ["-I", "-B"], str(REPO / "bin" / "desk-relay")))
        self.assertTrue(os.access(python, os.X_OK), python)
        self.assertTrue(Path(script).is_file())
        self.assertEqual(spec["Label"], relay_launchd.LABEL)
        v1 = host_launchd.job("pi", "mac", Path("/k"), Path("/Users/me"), "/python3", REPO)
        self.assertEqual(spec["EnvironmentVariables"], v1["EnvironmentVariables"])
        self.assertEqual((spec["RunAtLoad"], spec["KeepAlive"], spec["ThrottleInterval"], spec["ProcessType"]),
                         (True, True, 10, "Standard"))
        self.assertEqual((spec["StandardOutPath"], spec["StandardErrorPath"]), ("/dev/null", "/dev/null"))
        self.assertEqual(set(spec), {"Label", "ProgramArguments", "RunAtLoad", "KeepAlive", "ThrottleInterval",
                                     "ProcessType", "EnvironmentVariables", "StandardOutPath",
                                     "StandardErrorPath"})


class RelayInstallTest(RelayLaunchdCase):
    def test_relay_install_writes_plist_then_loads_only_its_label(self):
        self.add_key()
        code, out, err = self.cli(unload=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(plistlib.loads(self.launchd.plist.read_bytes()), relay_launchd.job(self.config))
        self.assertEqual(self.launchd.plist.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.fake.calls, [["launchctl", "bootout", SERVICE],
                                           ["launchctl", "bootstrap", DOMAIN, str(self.launchd.plist)]])
        self.assertIn(f"{relay_launchd.LABEL} loaded from {self.launchd.plist}", out)

    def test_relay_install_refuses_before_any_change(self):
        fake_repo = make_home(self)
        cases = {
            "off macOS": ({"platform": "linux"}, "runs only on the Mac"),
            "no link key": ({}, "llm-switchboard-desk-mac is missing"),
            "pi as the Mac's name": ({"environ": {MAC_NAME: "pi"}}, "not a Mac host name"),
            "a bad Mac name": ({"environ": {MAC_NAME: "Mac Book"}}, "not a Mac host name"),
            "no checkout interpreter": ({"config": DeskConfig.from_env(home=self.home, environ={},
                                                                       repo=fake_repo)}, ".venv/bin/python is missing"),
        }
        for name, (options, message) in cases.items():
            with self.subTest(name):
                if name == "no checkout interpreter":
                    self.add_key()
                code, out, err = self.cli(unload=False, **options)
                self.assertEqual(code, relay_cli_setup_exit(), err)
                self.assertIn(message, err)
                self.assertIn("nothing changed", err)
                self.assertEqual(self.fake.calls, [])
                self.assertFalse(self.agents.exists())

    def test_relay_install_reports_bootstrap_failure(self):
        self.add_key()
        failing = FakeLaunchctl()

        def refuse_bootstrap(argv, check=False, capture_output=False):
            if argv[1] == "bootstrap":
                failing.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 5, b"", b"")
            return failing(argv, check, capture_output)
        self.launchd = relay_launchd.Launchd(self.agents, DOMAIN, refuse_bootstrap)
        with mock.patch.object(host_launchd.time, "sleep"):
            code, out, err = self.cli(unload=False)
        self.assertEqual(code, 1)
        self.assertIn("launchctl bootstrap of dev.llm-switchboard.desk-relay failed (exit 5)", err)


class RelayUnloadTest(RelayLaunchdCase):
    def test_relay_install_unload_boots_out_only_its_label(self):
        self.install_loaded()
        other = self.agents / f"{host_launchd.LABEL}.plist"
        other.write_bytes(b"v1 job")
        code, out, err = self.cli(unload=True)
        self.assertEqual(code, 0, err)
        self.assertFalse(self.launchd.plist.exists())
        self.assertEqual(other.read_bytes(), b"v1 job")
        self.assertEqual({call[2] for call in self.fake.calls}, {SERVICE})
        self.assertEqual(self.fake.calls[0], ["launchctl", "bootout", SERVICE])
        self.assertIn("no desk relay is running", out)

    def test_relay_install_unload_done_only_once_relay_lock_free(self):
        """launchd takes two polls to drop the label, and the relay exits only
        after five: done comes after both, never before."""
        self.install_loaded()
        self.fake.print_lag = 2
        relay = self.hold_relay_lock()
        exited_at = None

        def relay_exits_at_fifth_poll():
            nonlocal exited_at
            if self.sleeps == 5:
                relay.stdin.close()
                relay.wait(timeout=30)
                exited_at = self.sleeps
        self.on_sleep = relay_exits_at_fifth_poll
        code, out, err = self.cli(unload=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(exited_at, 5)
        self.assertEqual(self.sleeps, 5)
        self.assertFalse(self.launchd.plist.exists())

    def test_relay_install_unload_exit_1_when_relay_keeps_lock(self):
        self.install_loaded()
        self.hold_relay_lock()
        start = self.clock()
        code, out, err = self.cli(unload=True)
        self.assertEqual(code, 1)
        elapsed = self.clock() - start
        self.assertGreaterEqual(elapsed, relay_launchd.UNLOAD_WAIT_SECONDS)
        self.assertLess(elapsed, relay_launchd.UNLOAD_WAIT_SECONDS + 2 * relay_launchd.POLL_SECONDS)
        self.assertIn(f"a desk relay is still running: it holds {self.config.relay_lock}", err)
        self.assertEqual(out, "")
        self.assertFalse(self.launchd.plist.exists())
        self.assertEqual(self.fake.calls[0], ["launchctl", "bootout", SERVICE])

    def test_relay_install_unload_unconfirmed_while_label_not_proved_gone(self):
        cases = {"still listed": (0, "launchd still lists dev.llm-switchboard.desk-relay"),
                 "print failed otherwise": (5, "`launchctl print` exited 5")}
        for name, (status, message) in cases.items():
            with self.subTest(name):
                self.fake.print_status = status
                code, out, err = self.cli(unload=True, wait_seconds=1)
                self.assertEqual(code, 1)
                self.assertIn(message, err)

    def test_relay_install_unload_idempotent(self):
        code, out, err = self.cli(unload=True)
        self.assertEqual(code, 0, err)
        code, out, err = self.cli(unload=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sleeps, 0)

    def test_relay_install_unload_refused_off_macos(self):
        self.install_loaded()
        code, out, err = self.cli(unload=True, platform="linux")
        self.assertEqual(code, relay_cli_setup_exit())
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(self.launchd.plist.exists())


def relay_cli_setup_exit() -> int:
    from llm_switchboard.desk.relay_cli import EXIT_SETUP
    return EXIT_SETUP


if __name__ == "__main__":
    unittest.main()
