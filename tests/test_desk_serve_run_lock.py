"""The run lock and `desk serve` (docs/voice-desk.md, "Hub side", the run
lock): the hub process holds `hub.lock` from before anything of the hub is
constructed until it exits, so no hub state is read while another hub could
still write it, and an incoming hub loads the lock table the outgoing one
left."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import unittest

from desk_fixtures import REPO, SESSION_A, SESSION_B, wait_until
from test_desk_operator import ChannelHubCase

from llm_switchboard.desk.config import PORT_ENV
from llm_switchboard.desk.hub import Hub
from llm_switchboard.desk.locks import LockStore, inspect_lock_file
from llm_switchboard.desk.model import Lock
from llm_switchboard.desk.run_lock import START_WAIT_SECONDS, RunLockHeld, hold_for_process, probe

# Generous: the Pi runs the suite at load 10 to 20.
WAIT_SECONDS = 60


def serve_env(home: Path) -> dict:
    """`desk serve` in a child, isolated: this HOME, an ephemeral port, and
    a tmux socket no server uses (never the supervisor's)."""
    return {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": "", "XDG_STATE_HOME": "", "XDG_RUNTIME_DIR": "",
            PORT_ENV: "0", "LLM_SWITCHBOARD_TMUX_SOCKET": f"desk-test-none-{os.getpid()}"}


def serve_argv() -> list[str]:
    return [sys.executable, "-m", "llm_switchboard", "desk", "serve"]


class ServeTakesRunLockTest(ChannelHubCase):
    """ChannelHubCase for its isolated home, cert and enrollment; its own
    in-process hub stands in for nothing here."""

    def hub_sockets(self) -> list[Path]:
        return [self.config.operator_socket, self.config.channel_socket, self.config.link_socket]

    def test_serve_takes_run_lock_before_constructing_hub(self):
        # A hub that read its state before the lock would hang opening this.
        self.hub.close()
        for path in self.hub_sockets():
            path.unlink(missing_ok=True)
        self.config.lock_file.unlink(missing_ok=True)
        os.mkfifo(self.config.lock_file)
        with probe(self.config.hub_run_lock):
            started = time.monotonic()
            done = subprocess.run(serve_argv(), cwd=REPO, env=serve_env(self.home), capture_output=True, text=True,
                                  timeout=WAIT_SECONDS)
            elapsed = time.monotonic() - started
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn("holds hub.lock", done.stderr)
        self.assertGreaterEqual(elapsed, START_WAIT_SECONDS)
        self.assertEqual([path for path in self.hub_sockets() if os.path.lexists(path)], [])
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.config.lock_file).st_mode))

    def test_a_running_serve_holds_the_run_lock_until_it_exits(self):
        self.hub.close()
        child = subprocess.Popen(serve_argv(), cwd=REPO, env=serve_env(self.home), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(child.kill)
        self.addCleanup(child.wait, WAIT_SECONDS)
        wait_until(lambda: self.config.operator_socket.exists() or child.poll() is not None, timeout=WAIT_SECONDS)
        self.assertIsNone(child.poll(), "desk serve exited at start")
        with self.assertRaises(RunLockHeld):
            with probe(self.config.hub_run_lock):
                pass
        child.terminate()
        self.assertEqual(child.wait(WAIT_SECONDS), 0)
        with probe(self.config.hub_run_lock):
            pass

    def test_serve_refuses_a_hub_without_its_run_lock(self):
        with self.assertRaises(TypeError):
            self.hub.serve(None)


class OutgoingHub:
    """A child process holding `hub.lock` as an outgoing hub does, until
    told to exit."""

    def __init__(self, case: unittest.TestCase, path: Path):
        code = ("import sys\n"
                "from llm_switchboard.desk.run_lock import hold_for_process\n"
                "hold_for_process(sys.argv[1])\n"
                "print('held', flush=True)\n"
                "sys.stdin.read()\n")
        self.child = subprocess.Popen([sys.executable, "-c", code, str(path)], cwd=REPO, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True)
        case.addCleanup(self.child.kill)
        case.addCleanup(self.child.wait, WAIT_SECONDS)
        assert self.child.stdout.readline() == "held\n"

    def exit(self) -> None:
        self.child.stdin.close()
        self.child.wait(WAIT_SECONDS)
        self.child.stdout.close()


class IncomingHubTest(ChannelHubCase):
    def test_incoming_serve_loads_locks_left_by_outgoing_hub(self):
        # The fixture's hub is the outgoing one; its process lock is played
        # by a child, since a run lock is released only by its process's exit.
        outgoing = self.hub
        outgoing.close()
        lock_file = self.config.lock_file
        lock_file.unlink(missing_ok=True)
        holder = OutgoingHub(self, self.config.hub_run_lock)
        incoming = {}

        def serve_in_order():
            # `desk serve`'s order: the run lock (waiting for the outgoing
            # hub's exit), then the hub, whose constructor loads locks.json.
            run_lock = hold_for_process(self.config.hub_run_lock, wait_seconds=WAIT_SECONDS)
            incoming["hub"] = self.start_hub()
            incoming["run_lock"] = run_lock
        thread = threading.Thread(target=serve_in_order, daemon=True)
        thread.start()
        # The outgoing hub's handoff persists a lock while the incoming one waits.
        left = Lock(lock_id="a" * 16, host="pi", session_id=SESSION_A, request_id="0" * 32 + "." + "1" * 16,
                    ticket_hash="b" * 64, since=time.time(), channel_pid=4242, channel_start="123")
        LockStore(lock_file).add(left)
        self.assertNotIn("hub", incoming)
        holder.exit()
        thread.join(WAIT_SECONDS)
        self.hub = incoming["hub"]
        self.assertEqual(self.target(SESSION_A)["lock"]["lock_id"], left.lock_id)
        _, generation_a = self.registered(SESSION_A)
        self.assertRefused(409, "busy", self.hub.ticket,
                           {"session_id": SESSION_A, "generation": generation_a, "epoch": self.hub.epoch}, self.token)
        _, generation_b = self.registered(SESSION_B)
        self.send(SESSION_B, generation_b)
        self.assertEqual({lock.session_id for lock in inspect_lock_file(lock_file)}, {SESSION_A, SESSION_B})


if __name__ == "__main__":
    unittest.main()
