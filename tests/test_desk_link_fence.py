"""The handoff gate, `link_drop` and the rollback fence (docs/voice-desk.md,
"Hub side": the handoff gate and the run lock; "Switching it off,
revocation and rollback", step 3), with a real hub driven by the test-side
relay in tests/link_peer.py and `desk link-fence` run as a child process."""

import contextlib
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from desk_fixtures import SESSION_A, desk_config, make_home, request_id, run_desk_cli, wait_until
from link_peer import MAC, MAC2, write_signers
from test_desk_host_enrollment import mac_lock
from test_desk_link_channels import SESSION_M, MacTargetCase

from llm_switchboard.desk import locks
from llm_switchboard.desk.locks import MAX_LOCK_FILE_BYTES, LockStore
from llm_switchboard.desk.link_authorize import run_lock_refusal
from llm_switchboard.desk.link_protocol import LinkType
from llm_switchboard.desk.model import DeskError, Lock
from llm_switchboard.desk.refusals import Refusal
from llm_switchboard.desk.run_lock import probe

RUN_LOCK_REFUSAL = run_lock_refusal("link-fence")


def pi_lock(config) -> Lock:
    lock = Lock("fedcba9876543210", "pi", SESSION_A, request_id(2), "f" * 64, 1_800_000_000.0, 4321, "777")
    store = LockStore(config.lock_file)
    store.load()
    store.add(lock)
    return lock


@contextlib.contextmanager
def run_lock_held_by_child(config):
    """`hub.lock` held by another process, as a starting or stopping hub's."""
    script = ("import fcntl, os, sys\n"
              "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_CREAT, 0o600)\n"
              "fcntl.flock(fd, fcntl.LOCK_EX)\n"
              "print('held', flush=True)\n"
              "sys.stdin.read()\n")
    config.hub_run_lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    child = subprocess.Popen([sys.executable, "-I", "-c", script, str(config.hub_run_lock)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        if child.stdout.readline().strip() != "held":
            raise AssertionError("the child did not take hub.lock")
        yield
    finally:
        child.stdin.close()
        child.wait(timeout=30)
        child.stdout.close()


class Paused:
    """Patches `target.name` so its first call from a thread other than the
    test's waits, after `entered`, until `resume` is set."""

    def __init__(self, case, target, name):
        self.entered, self.resume = threading.Event(), threading.Event()
        original, test_thread, paused = getattr(target, name), threading.current_thread(), []

        def wrapper(*args, **kwargs):
            if threading.current_thread() is not test_thread and not paused:
                paused.append(True)
                self.entered.set()
                if not self.resume.wait(60):
                    raise AssertionError("never resumed")
            return original(*args, **kwargs)
        patcher = patch.object(target, name, wrapper)
        patcher.start()
        case.addCleanup(patcher.stop)
        case.addCleanup(self.resume.set)


class FenceCase(MacTargetCase):
    def fence(self) -> dict:
        return self.hub.handle_operator({"op": "link_fence"})

    def fence_cli(self, home=None):
        return run_desk_cli(home or self.home, "link-fence", input="")

    def send_in_thread(self) -> dict:
        result = {}

        def run():
            try:
                result["value"] = self.send_m()
            except DeskError as exc:
                result["error"] = exc
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        result["thread"] = thread
        return result

    def retire_without_finalizing(self):
        """The link retires (under the links lock); its registrations are
        dropped only by `finalize`, which the test runs later."""
        link = self.hub.links.live_link(MAC)
        with self.hub.links._lock:
            self.hub.links._retire_locked(link, "test")
        return link

    def persisted(self) -> list[str]:
        return [lock.lock_id for lock in LockStore(self.config.lock_file).load()]


class FenceOnlineTest(FenceCase):
    def test_link_fence_requires_no_signer_no_link_no_mac_lock(self):
        sent = self.send_m()
        self.mac.expect(LinkType.EVENT)
        self.mac.send(LinkType.DELIVERED, chan=1, request_id=sent["request_id"])
        wait_until(lambda: sent["request_id"] in self.hub.state._writer_done)
        self.assertEqual(self.fence(), {"ok": False, "error": Refusal.FENCE_SIGNERS_LISTED, "hosts": [MAC, MAC2]})
        done = self.fence_cli()
        self.assertEqual(done.returncode, 1)
        self.assertIn(Refusal.FENCE_SIGNERS_LISTED, done.stderr)
        write_signers(self.config.link_signers_file)
        # The fence reads; it retires nothing.
        self.assertEqual(self.fence(), {"ok": False, "error": Refusal.FENCE_LINK_LIVE, "hosts": [MAC]})
        self.assertEqual(self.links()[MAC]["state"], "up")
        answer = self.hub.handle_operator({"op": "link_drop", "host": MAC})
        self.assertEqual(answer, {"ok": True, "readable": True, "signer": None, "link": None})
        self.assertTrue(self.mac.closed())
        lock = self.lock_m()
        self.assertEqual(self.fence(), {"ok": False, "error": Refusal.FENCE_MAC_LOCK, "locks": [
            {"host": MAC, "session_id": SESSION_M, "lock_id": lock["lock_id"]}]})
        done = self.fence_cli()
        self.assertEqual(done.returncode, 1)
        self.assertIn(lock["lock_id"], done.stderr)
        self.assertEqual(self.clear_m(), {"cleared": True})  # the reported delivery needs no presence
        self.assertEqual(self.fence(), {"ok": True, "fenced": True})
        done = self.fence_cli()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("link-fence passed", done.stdout)

    def test_link_drop_answer_reflects_reread(self):
        write_signers(self.config.link_signers_file, self.key("mac-rotated").signer, self.key("mac2").signer)
        answer = self.hub.handle_operator({"op": "link_drop", "host": MAC})
        self.assertEqual(answer, {"ok": True, "readable": True, "link": None,
                                  "signer": {"fingerprint": self.key("mac-rotated").signer.fingerprint}})
        self.assertTrue(self.mac.closed())
        self.assertEqual(self.hub.state.registrations(), [])
        other = self.connect("mac2", MAC2)
        answer = self.hub.handle_operator({"op": "link_drop", "host": MAC2})
        fingerprint = {"fingerprint": self.key("mac2").signer.fingerprint}
        self.assertEqual((answer["signer"], answer["link"]), (fingerprint, fingerprint))
        self.assertEqual(self.links()[MAC2]["state"], "up")
        for host in ("pi", "", None, "Bad Host"):
            self.assertEqual(self.hub.handle_operator({"op": "link_drop", "host": host}),
                             {"ok": False, "error": Refusal.BAD_HOST})

    def test_handoff_gate_refuses_retiring_registration(self):
        captured, original_prepare, original_gate = {}, self.hub._prepare_handoff, self.hub.links.handoff_gate

        def prepare(request):
            captured["request_id"] = request.request_id
            return original_prepare(request)

        def gate(identity):
            # After the dispatch recheck passed: the link retires, its
            # registrations not yet dropped.
            captured["link"] = self.retire_without_finalizing()
            captured["registered"] = len(self.hub.state.registrations())
            return original_gate(identity)
        with patch.object(self.hub, "_prepare_handoff", prepare), patch.object(self.hub.links, "handoff_gate", gate):
            with self.assertRaises(DeskError) as raised:
                self.send_m()
        self.assertEqual((raised.exception.status, raised.exception.reason), (409, Refusal.TARGET_CHANGED_NOT_SENT))
        self.assertEqual(captured["registered"], 1)
        self.assertEqual(self.hub.state.request_state(captured["request_id"]), "expired")
        self.assertEqual(self.persisted(), [])
        self.assertIsNone(self.target(SESSION_M).get("lock"))
        self.hub.links._finalize(captured["link"])
        write_signers(self.config.link_signers_file)
        self.assertEqual(self.fence(), {"ok": True, "fenced": True})

    def test_link_fence_refuses_handoff_between_recheck_and_lock_write(self):
        paused = Paused(self, self.hub.state._lock_store, "add")
        send = self.send_in_thread()
        self.assertTrue(paused.entered.wait(30))
        link = self.retire_without_finalizing()
        write_signers(self.config.link_signers_file)
        self.assertEqual(self.fence(), {"ok": False, "error": Refusal.FENCE_HANDOFF})
        paused.resume.set()
        send["thread"].join(30)
        self.assertFalse(send["thread"].is_alive())
        self.hub.links._finalize(link)
        [lock_id] = self.persisted()
        self.assertEqual(self.fence(), {"ok": False, "error": Refusal.FENCE_MAC_LOCK, "locks": [
            {"host": MAC, "session_id": SESSION_M, "lock_id": lock_id}]})

    def test_link_fence_consistent_with_concurrent_pi_lock_write(self):
        self.hub.handle_operator({"op": "link_drop", "host": MAC})
        write_signers(self.config.link_signers_file)
        self.hub.handle_operator({"op": "link_drop", "host": MAC})
        pi = self.channel(SESSION_A)
        wait_until(lambda: self.target(SESSION_A).get("generation") is not None)
        paused = Paused(self, locks, "_fsync_dir")  # after the rename, under the state's lock
        target = self.target(SESSION_A)
        ticket = self.hub.ticket({"session_id": SESSION_A, "generation": target["generation"],
                                  "epoch": self.hub.epoch}, self.token)["ticket"]
        sent = {}
        sender = threading.Thread(target=lambda: sent.update(self.hub.send({"ticket": ticket, "text": "pi"},
                                                                         self.token)), daemon=True)
        sender.start()
        self.assertTrue(paused.entered.wait(30))
        fenced = {}
        fence = threading.Thread(target=lambda: fenced.update(self.fence()), daemon=True)
        fence.start()
        fence.join(0.5)
        self.assertTrue(fence.is_alive())  # it waits for the state's lock
        paused.resume.set()
        fence.join(30)
        sender.join(30)
        self.assertEqual(fenced, {"ok": True, "fenced": True})
        self.assertEqual(sent["state"], "handed_off")
        self.assertEqual(pi.read()[0].value, "event")
        [lock] = LockStore(self.config.lock_file).load()
        self.assertEqual((lock.host, lock.session_id), ("pi", SESSION_A))
        self.assertIsNone(self.hub.state._lock_error)
        self.assertEqual(list(self.hub.state._locks), [lock.lock_id])

    def test_link_fence_after_replace_then_dir_fsync_failure(self):
        calls = []
        original = locks._fsync_dir

        def fail_once(directory):
            if not calls:
                calls.append(directory)
                raise OSError("directory fsync failed")
            return original(directory)
        with patch.object(locks, "_fsync_dir", fail_once):
            with self.assertRaises(DeskError) as raised:
                self.send_m()
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(len(self.persisted()), 1)  # the file was replaced
        self.assertIsNone(self.target(SESSION_M).get("lock"))  # the hub's list lacks it
        self.mac.send(LinkType.PING, n=7)
        self.assertEqual(self.mac.expect(LinkType.PONG), {"n": 7})  # no event came first
        write_signers(self.config.link_signers_file)
        self.hub.handle_operator({"op": "link_drop", "host": MAC})
        answer = self.fence()
        self.assertEqual((answer["ok"], answer["error"]), (False, Refusal.FENCE_STORE_ERROR))
        self.hub.close()
        done = self.fence_cli()
        self.assertEqual(done.returncode, 1)
        self.assertIn(Refusal.FENCE_MAC_LOCK, done.stderr)

    def test_link_fence_lock_file_not_regular_refuses_promptly(self):
        write_signers(self.config.link_signers_file)
        self.hub.handle_operator({"op": "link_drop", "host": MAC})
        before = (list(self.hub.state._locks), self.hub.state._lock_error)
        target = self.home / "elsewhere.json"
        target.write_text('{"version": 1, "locks": []}')
        for make in (os.mkfifo, lambda path: path.symlink_to(target)):
            with self.subTest(make=make):
                with contextlib.suppress(FileNotFoundError):
                    self.config.lock_file.unlink()
                make(self.config.lock_file)
                answer = self.fence()
                self.assertEqual((answer["ok"], answer["error"]), (False, Refusal.FENCE_LOCK_FILE))
                done = self.fence_cli()
                self.assertEqual(done.returncode, 1)
                self.assertIn(Refusal.FENCE_LOCK_FILE, done.stderr)
                self.assertEqual((list(self.hub.state._locks), self.hub.state._lock_error), before)

    def test_link_fence_refuses_unreadable_or_corrupt_lock_store(self):
        write_signers(self.config.link_signers_file)
        self.hub.handle_operator({"op": "link_drop", "host": MAC})
        self.assertEqual(list(self.hub.state._locks), [])
        offline = make_home(self)
        config = desk_config(offline)
        write_signers(config.link_signers_file)
        for name, data, mode in (("unreadable", b'{"version": 1, "locks": []}', 0),
                                 ("oversize", b" " * (MAX_LOCK_FILE_BYTES + 1), 0o600),
                                 ("malformed", b"{", 0o600)):
            with self.subTest(name):
                for path in (self.config.lock_file, config.lock_file):
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with contextlib.suppress(FileNotFoundError):
                        path.chmod(0o600)
                        path.unlink()
                    path.write_bytes(data)
                    path.chmod(mode)
                answer = self.fence()
                self.assertEqual((answer["ok"], answer["error"]), (False, Refusal.FENCE_LOCK_FILE))
                done = self.fence_cli(offline)
                self.assertEqual(done.returncode, 1)
                self.assertIn(Refusal.FENCE_LOCK_FILE, done.stderr)

    def test_link_fence_refuses_unreadable_signers(self):
        self.config.link_signers_file.write_text("garbage\n")
        self.assertEqual(self.fence(), {"ok": False, "error": Refusal.FENCE_SIGNERS_UNREADABLE})
        offline = make_home(self)
        config = desk_config(offline)
        config.link_signers_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.link_signers_file.write_text("garbage\n")
        done = self.fence_cli(offline)
        self.assertEqual(done.returncode, 1)
        self.assertIn(Refusal.FENCE_SIGNERS_UNREADABLE, done.stderr)


class FenceStoppingHubTest(FenceCase):
    def test_link_fence_offline_refuses_while_gated_handler_paused(self):
        with contextlib.ExitStack() as held:
            held.enter_context(probe(self.config.hub_run_lock))  # as `desk serve` holds it
            paused = Paused(self, self.hub.state._lock_store, "add")  # inside the gate, at the lock write
            send = self.send_in_thread()
            self.assertTrue(paused.entered.wait(30))
            write_signers(self.config.link_signers_file)
            # A stopping hub: its operator socket is gone while the handler
            # still runs. (`Hub.close` itself waits for the state's lock,
            # which the lock write holds.)
            self.hub.operator.close()
            done = self.fence_cli()
            self.assertEqual(done.returncode, 1)
            self.assertIn(RUN_LOCK_REFUSAL, done.stderr)
            paused.resume.set()
            send["thread"].join(30)
            self.assertFalse(send["thread"].is_alive())
            [lock_id] = self.persisted()
            self.hub.close()
        done = self.fence_cli()
        self.assertEqual(done.returncode, 1)
        self.assertIn(Refusal.FENCE_MAC_LOCK, done.stderr)
        self.assertIn(lock_id, done.stderr)


class FenceOfflineTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.config = desk_config(self.home)

    def fence(self):
        return run_desk_cli(self.home, "link-fence", input="")

    def test_link_fence_offline(self):
        done = self.fence()  # no files at all: nothing listed, no lock
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no hub running", done.stdout)
        pi_lock(self.config)
        write_signers(self.config.link_signers_file)
        self.assertEqual(self.fence().returncode, 0)
        from link_peer import KeyPair
        write_signers(self.config.link_signers_file, KeyPair(self.home, MAC).signer)
        done = self.fence()
        self.assertEqual(done.returncode, 1)
        self.assertIn(Refusal.FENCE_SIGNERS_LISTED, done.stderr)
        self.assertIn(f"host {MAC}", done.stderr)
        write_signers(self.config.link_signers_file)
        lock = mac_lock(self.config, SESSION_M, "SHA256:" + "A" * 43)
        before = self.config.lock_file.read_bytes()
        done = self.fence()
        self.assertEqual(done.returncode, 1)
        self.assertIn(lock.lock_id, done.stderr)
        self.assertEqual(self.config.lock_file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
