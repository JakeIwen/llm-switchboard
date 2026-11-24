"""Locks by host (docs/voice-desk.md, "Locks bound to the Mac that wrote
them"): a `pi` lock has exactly phase 1's fields, any other host's lock
those plus `link_key` and `machine`; a phase 1 store fails closed on a Mac
lock; `inspect_lock_file` reads the file without any store and never blocks."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from desk_phase1 import phase1_root, run_phase1

from llm_switchboard.desk.locks import LockStore, inspect_lock_file
from llm_switchboard.desk.model import HOST, MAX_LOCK_FILE_BYTES, Lock, LockStoreError, is_desk_host

SESSION = "11111111-2222-4333-8444-555555555555"
REQUEST_ID = "0" * 32 + "." + "1" * 16
PHASE1_FIELDS = {"lock_id", "host", "session_id", "request_id", "ticket_hash", "since", "channel_pid",
                 "channel_start"}
LINK_KEY = "SHA256:" + "A" * 42 + "c"
MACHINE = "0123456789abcdef" * 2
# Generous: the Pi runs the suite at load 10 to 20.
PROMPT_SECONDS = 30


def pi_lock(lock_id: str = "a" * 16) -> Lock:
    return Lock(lock_id=lock_id, host=HOST, session_id=SESSION, request_id=REQUEST_ID, ticket_hash="b" * 64,
                since=1700000000.5, channel_pid=4242, channel_start="123456")


def mac_lock(lock_id: str = "c" * 16) -> Lock:
    return replace(pi_lock(lock_id), host="mac", link_key=LINK_KEY, machine=MACHINE)


def file_of(*entries: dict) -> bytes:
    return json.dumps({"version": 1, "locks": list(entries)}).encode()


class LockFieldsTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "locks.json"

    def load(self, data: bytes) -> list[Lock]:
        self.path.write_bytes(data)
        return LockStore(self.path).load()

    def test_lock_fields_by_host(self):
        self.assertEqual(set(pi_lock().to_json()), PHASE1_FIELDS)
        self.assertEqual(set(mac_lock().to_json()), PHASE1_FIELDS | {"link_key", "machine"})
        store = LockStore(self.path)
        store.add(pi_lock())
        store.add(mac_lock())
        self.assertEqual(LockStore(self.path).load(), [pi_lock(), mac_lock()])
        self.assertEqual(inspect_lock_file(self.path), [pi_lock(), mac_lock()])

    def test_a_pi_lock_with_link_fields_is_refused(self):
        for extra in ({"link_key": LINK_KEY}, {"machine": MACHINE}, {"link_key": LINK_KEY, "machine": MACHINE}):
            with self.subTest(extra=extra):
                with self.assertRaises(LockStoreError):
                    LockStore(self.dir / "new.json").add(replace(pi_lock(), **extra))
                with self.assertRaises(LockStoreError):
                    self.load(file_of({**pi_lock().to_json(), **extra}))

    def test_a_mac_lock_needs_both_link_fields_well_formed(self):
        bad = ({"link_key": None}, {"machine": None}, {"link_key": "SHA256:short"}, {"link_key": "MD5:" + "a" * 43},
               {"machine": "0" * 31}, {"machine": "G" * 32}, {"link_key": 7})
        for change in bad:
            with self.subTest(change=change):
                with self.assertRaises(LockStoreError):
                    LockStore(self.dir / "new.json").add(replace(mac_lock(), **change))
                with self.assertRaises(LockStoreError):
                    self.load(file_of({**mac_lock().to_json(), **change}))
        for missing in ("link_key", "machine"):
            with self.subTest(missing=missing):
                entry = mac_lock().to_json()
                del entry[missing]
                with self.assertRaises(LockStoreError):
                    self.load(file_of(entry))

    def test_host_must_be_a_desk_host(self):
        for host in ("", "Mac", "-mac", "a" * 33, "mac.lan", 7, None):
            with self.subTest(host=host):
                self.assertFalse(is_desk_host(host))
                with self.assertRaises(LockStoreError):
                    self.load(file_of({**mac_lock().to_json(), "host": host}))
        for host in (HOST, "mac", "m4-mac"):
            self.assertTrue(is_desk_host(host))


class Phase1StoreTest(unittest.TestCase):
    def test_phase1_lock_store_reading_a_mac_lock_fails_closed(self):
        root = phase1_root(self)
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "locks.json"
        store = LockStore(path)
        store.add(pi_lock())
        store.add(mac_lock())
        before = path.read_bytes()
        code = """
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import LockStoreError
store = LockStore(sys.argv[1])
try:
    store.load()
    print("loaded")
except LockStoreError:
    try:
        store.remove("a" * 16)
        print("removed")
    except LockStoreError:
        print("refused")
"""
        done = run_phase1(root, code, str(path))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "refused"), done.stderr)
        self.assertEqual(path.read_bytes(), before)


class InspectLockFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "locks.json"

    def inspect_promptly(self):
        out = {}

        def run():
            try:
                out["locks"] = inspect_lock_file(self.path)
            except LockStoreError as exc:
                out["error"] = exc
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(PROMPT_SECONDS)
        self.assertFalse(thread.is_alive(), "inspect_lock_file blocked")
        return out

    def test_missing_file_is_empty(self):
        self.assertEqual(self.inspect_promptly(), {"locks": []})

    def test_unusable_files_raise_promptly_and_touch_no_store(self):
        store = LockStore(self.path)
        store.add(pi_lock())
        target = self.dir / "elsewhere.json"
        target.write_bytes(self.path.read_bytes())
        cases = {
            "fifo": lambda: os.mkfifo(self.path),
            "symlink": lambda: self.path.symlink_to(target),
            "oversize": lambda: self.path.write_bytes(b" " * (MAX_LOCK_FILE_BYTES + 1)),
            "malformed": lambda: self.path.write_bytes(b'{"version": 1, "locks": [{"lock_id": 1}]}'),
            "not json": lambda: self.path.write_bytes(b"{nope"),
            "directory": lambda: self.path.mkdir(),
        }
        for name, make in cases.items():
            with self.subTest(case=name):
                if self.path.is_dir() and not self.path.is_symlink():
                    self.path.rmdir()
                else:
                    self.path.unlink(missing_ok=True)
                make()
                out = self.inspect_promptly()
                self.assertIsInstance(out.get("error"), LockStoreError)
        # The live store kept its list and isn't broken: it can still write.
        self.path.rmdir()
        store.add(mac_lock())
        self.assertEqual(inspect_lock_file(self.path), [pi_lock(), mac_lock()])


if __name__ == "__main__":
    unittest.main()
