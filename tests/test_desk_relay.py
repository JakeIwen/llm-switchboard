"""The relay process (docs/voice-desk.md, "Processes, sockets and files",
"Bounds"): bin/desk-relay's launch log and its cap across a crash loop,
the launch log closed once logging runs, `relay.lock`, and what a stop
does to the link and the channel socket.

Every relay here runs under an isolated HOME. The ones started as real
processes carry an invalid Pi alias (LLM_SWITCHBOARD_PI_SSH), so even a relay
that got past the point a test expects it to stop at would refuse its ssh
route before running any child; none reaches launchd or the Pi.
"""

import functools
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from desk_fixtures import desk_config, make_home
from link_peer import MACHINE
from relay_fixture import FakeHub, MacHome, fake_sign, pump_invocation
from llm_switchboard.desk import mac_machine, relay
from llm_switchboard.desk.relay_link import RelayConnector
from llm_switchboard.desk.run_lock import probe

REPO = Path(__file__).resolve().parents[1]
GENEROUS = 30.0
# The design's bound ("Bounds": 256 KiB plus the output of one failed start).
LAUNCH_LOG_CAP = 256 * 1024
FAILING_RELAY = (
    "import sys\n"
    "sys.stderr.write('a failed start says a lot\\n' * 2000)\n"
    "raise ImportError('relay module failed to import')\n"
)
DRIVER = '''
import os, runpy, sys, threading
repo, ready = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
from llm_switchboard.desk import relay


class Connector:
    """Starts like a relay's connector, then fails a thread and writes to fd 2."""

    def __init__(self, config, channels, *, machine, relay_build):
        self.stopped = threading.Event()

    def run(self):
        def fail():
            raise RuntimeError("thread failure after start")
        thread = threading.Thread(target=fail, name="failing")
        thread.start()
        thread.join()
        os.write(2, b"fd 2 after logging started\\n")
        sys.stderr.write("sys.stderr after logging started\\n")
        sys.stderr.flush()
        open(ready, "w").close()
        self.stopped.wait()

    def stop(self):
        self.stopped.set()


relay.RelayConnector = Connector
relay.mac_machine.machine_id = lambda: "0" * 32
sys.argv = [os.path.join(repo, "bin", "desk-relay")]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def relay_env(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LLM_SWITCHBOARD_PI_SSH": "!no-route"}


def wait_for(predicate, timeout: float = GENEROUS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.05)


class LaunchLogTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.config = desk_config(self.home)
        self.launch_log = self.config.relay_launch_log

    def failing_checkout(self) -> Path:
        """A checkout whose bin/desk-relay is the real one and whose relay module fails to import."""
        checkout = self.home / "checkout"
        (checkout / "bin").mkdir(parents=True)
        shutil.copy2(REPO / "bin" / "desk-relay", checkout / "bin" / "desk-relay")
        package = checkout / "llm_switchboard" / "desk"
        package.mkdir(parents=True)
        (checkout / "llm_switchboard" / "__init__.py").write_text("")
        (package / "__init__.py").write_text("")
        (package / "relay.py").write_text(FAILING_RELAY)
        return checkout

    def start_failing(self, checkout: Path) -> None:
        done = subprocess.run([sys.executable, "-I", "-B", str(checkout / "bin" / "desk-relay")],
                              env=relay_env(self.home), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=GENEROUS)
        self.assertEqual(done.returncode, 1)

    def test_relay_launch_log_capped_across_crash_loop(self):
        checkout = self.failing_checkout()
        self.start_failing(checkout)
        one_start = self.launch_log.stat().st_size
        self.assertIn(b"ImportError: relay module failed to import", self.launch_log.read_bytes())
        sizes = []
        for _ in range(12):
            self.start_failing(checkout)
            sizes.append(self.launch_log.stat().st_size)
            self.assertLessEqual(sizes[-1], LAUNCH_LOG_CAP + one_start)
        # Without the cap the loop would have written far past it.
        self.assertGreater(13 * one_start, LAUNCH_LOG_CAP + one_start)
        self.assertTrue(any(after < before for before, after in zip(sizes, sizes[1:])), sizes)
        self.assertTrue(self.launch_log.read_bytes().endswith(b"ImportError: relay module failed to import\n"))

    def test_launch_log_truncated_only_when_over_cap(self):
        checkout = self.failing_checkout()
        self.launch_log.parent.mkdir(parents=True)
        self.launch_log.write_bytes(b"k" * LAUNCH_LOG_CAP)
        self.start_failing(checkout)
        self.assertTrue(self.launch_log.read_bytes().startswith(b"k" * LAUNCH_LOG_CAP))
        self.launch_log.write_bytes(b"k" * (LAUNCH_LOG_CAP + 1))
        self.start_failing(checkout)
        self.assertTrue(self.launch_log.read_bytes().startswith(b"a failed start says a lot\n"))

    def test_launch_log_closed_after_logging_starts(self):
        self.launch_log.parent.mkdir(parents=True)
        earlier = b"an earlier failed start\n"
        self.launch_log.write_bytes(earlier)
        driver, ready = self.home / "driver.py", self.home / "ready"
        driver.write_text(DRIVER)
        proc = subprocess.Popen([sys.executable, "-I", "-B", str(driver), str(REPO), str(ready)],
                                env=relay_env(self.home), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        wait_for(ready.exists)
        self.assertTrue(self.config.channel_socket.exists())
        log = self.config.relay_log.read_text()
        self.assertIn("uncaught exception in thread failing", log)
        self.assertIn("RuntimeError: thread failure after start", log)
        self.assertNotIn("after logging started", log)
        self.assertEqual(self.launch_log.read_bytes(), earlier)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(GENEROUS), 0)
        self.assertFalse(self.config.channel_socket.exists())
        self.assertIn("desk relay stopping", self.config.relay_log.read_text())
        self.assertEqual(self.launch_log.read_bytes(), earlier)


class RelayLockTest(unittest.TestCase):
    def test_second_relay_refused_by_relay_lock(self):
        """bin/desk-relay exits 3 while relay.lock is held, having bound
        nothing and opened no log of its own."""
        home = make_home(self)
        config = desk_config(home)
        with probe(config.relay_lock):
            procs = [subprocess.Popen(argv, env=relay_env(home), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
                     for argv in ([sys.executable, "-I", "-B", str(REPO / "bin" / "desk-relay")],)]
            for proc in procs:
                proc.communicate(timeout=GENEROUS)
            self.assertEqual([proc.returncode for proc in procs], [3])
        self.assertIn(b"another desk relay holds relay.lock", config.relay_launch_log.read_bytes())
        self.assertFalse(config.channel_socket.exists())
        self.assertFalse(config.relay_log.exists())


class _Returns:
    """A connector whose loop ends on its own (a dying link loop)."""

    def __init__(self, config, channels, **identity):
        self.stopped = False

    def run(self):
        return None

    def stop(self):
        self.stopped = True


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.mac = MacHome(self)
        self.config = self.mac.config
        self.stop = threading.Event()
        self.result: list[int] = []

    def serve(self, **options) -> threading.Thread:
        options.setdefault("machine", MACHINE)
        thread = threading.Thread(target=lambda: self.result.append(relay.serve(self.config, self.stop, **options)),
                                  daemon=True)
        thread.start()
        self.addCleanup(self.stop.set)
        return thread

    def assert_socket_closed(self) -> None:
        self.assertFalse(self.config.channel_socket.exists())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            with self.assertRaises(OSError):
                client.connect(str(self.config.channel_socket))

    def test_stop_retires_link_and_closes_channel_socket(self):
        hub = FakeHub(self.config.link_socket)
        self.addCleanup(hub.close)
        connector = functools.partial(RelayConnector, prepare=lambda config: pump_invocation(self.mac.home),
                                      sign=fake_sign, reader=self.mac.kernel)
        thread = self.serve(relay_build="0123abcd", connector_factory=connector)
        hello = hub.handshake()
        self.assertEqual((hello["relay_build"], hello["machine"]), ("0123abcd", MACHINE))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.config.channel_socket))
        self.stop.set()
        thread.join(GENEROUS)
        self.assertEqual(self.result, [0])
        self.assertTrue(hub.closed())
        self.assert_socket_closed()

    def test_link_loop_ending_ends_relay_with_failure(self):
        made = []
        thread = self.serve(connector_factory=lambda *a, **k: made.append(_Returns(*a, **k)) or made[-1])
        thread.join(GENEROUS)
        self.assertEqual(self.result, [relay.EXIT_FAILED])
        self.assertTrue(made[0].stopped)
        self.assert_socket_closed()

    def test_channel_socket_in_use_refuses_start(self):
        self.config.channel_socket.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(other.close)
        other.bind(str(self.config.channel_socket))
        other.listen(1)
        made = []
        thread = self.serve(connector_factory=lambda *a, **k: made.append(a))
        thread.join(GENEROUS)
        self.assertEqual((self.result, made), ([relay.EXIT_SETUP], []))
        self.assertTrue(self.config.channel_socket.exists())

    def test_unreadable_machine_refuses_start(self):
        made = []
        with mock.patch.object(mac_machine, "machine_id", side_effect=OSError("no uuid")):
            thread = self.serve(machine=None, connector_factory=lambda *a, **k: made.append(a))
            thread.join(GENEROUS)
        self.assertEqual((self.result, made), ([relay.EXIT_SETUP], []))
        self.assertFalse(self.config.channel_socket.exists())


if __name__ == "__main__":
    unittest.main()
