"""The lock file and the enrollment/settings files: durability, modes, caps
and refusing to trust anything unreadable."""

import json
import os
import secrets
import stat
import unittest

from desk_fixtures import SESSION_A, SESSION_B, Clock, FakeIdentity, desk_config, make_home

from llm_switchboard.desk.enrollment import DeskSettings, Enrollment
from llm_switchboard.desk.hub_state import HubState
from llm_switchboard.desk.locks import LockStore, LockStoreError
from llm_switchboard.desk.model import MAX_ENROLLED, MAX_LOCKS, DeskError, Lock


def lock(n: int, session_id: str = SESSION_A) -> Lock:
    return Lock(f"{n:016x}", "pi", session_id, f"epoch.{n}", "ab" * 32, 1000.0 + n)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class LockStoreTest(unittest.TestCase):
    def setUp(self):
        self.config = desk_config(make_home(self))
        self.path = self.config.lock_file

    def test_locks_round_trip_with_private_modes(self):
        store = LockStore(self.path)
        self.assertEqual(store.load(), [])
        store.add(lock(1))
        store.add(lock(2, SESSION_B))
        self.assertEqual(mode(self.path), 0o600)
        self.assertEqual(mode(self.path.parent), 0o700)
        self.assertEqual(LockStore(self.path).load(), [lock(1), lock(2, SESSION_B)])
        store.remove(lock(1).lock_id)
        self.assertEqual(LockStore(self.path).load(), [lock(2, SESSION_B)])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["locks.json"])

    def test_corrupt_or_oversize_files_refuse_loads_and_writes(self):
        self.path.parent.mkdir(parents=True)
        too_many = {"version": 1, "locks": [lock(n).to_json() for n in range(MAX_LOCKS + 1)]}
        bad_entry = {"version": 1, "locks": [{**lock(1).to_json(), "since": "soon"}]}
        for content in ("{not json", json.dumps({"locks": []}), json.dumps(too_many), json.dumps(bad_entry)):
            with self.subTest(content=content[:30]):
                self.path.write_text(content)
                store = LockStore(self.path)
                with self.assertRaises(LockStoreError):
                    store.load()
                with self.assertRaises(LockStoreError):
                    store.add(lock(5))
                self.assertEqual(self.path.read_text(), content)

    @unittest.skipIf(os.geteuid() == 0, "root reads files whatever their mode")
    def test_unreadable_and_unwritable_files(self):
        store = LockStore(self.path)
        store.load()
        store.add(lock(1))
        os.chmod(self.path, 0)
        self.addCleanup(os.chmod, self.path, 0o600)
        with self.assertRaisesRegex(LockStoreError, "unreadable"):
            LockStore(self.path).load()
        os.chmod(self.path, 0o600)
        # Something in the way of the temp file makes the write fail.
        blocker = self.path.parent / f".{self.path.name}.tmp"
        (blocker / "x").mkdir(parents=True)
        with self.assertRaisesRegex(LockStoreError, "unwritable"):
            store.add(lock(2))
        # A failed add changes nothing, in memory or on disk.
        (blocker / "x").rmdir()
        blocker.rmdir()
        self.assertEqual(LockStore(self.path).load(), [lock(1)])
        store.add(lock(3))
        self.assertEqual(LockStore(self.path).load(), [lock(1), lock(3)])

    def test_the_cap_is_enforced_on_add(self):
        store = LockStore(self.path)
        store.load()
        for n in range(MAX_LOCKS):
            store.add(lock(n))
        with self.assertRaisesRegex(LockStoreError, "full"):
            store.add(lock(999))

    def test_a_hub_over_a_corrupt_lock_file_refuses_sends_and_reports_it(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("garbage")
        state = HubState(LockStore(self.path), now=Clock(), token_bytes=secrets.token_bytes)
        state.set_sends_enabled(True)
        generation = state.register(1, SESSION_A, FakeIdentity(1, "1", 2, "2", SESSION_A))
        self.assertIn("corrupt", state.snapshot()["lock_store_error"])
        with self.assertRaises(DeskError) as caught:
            state.issue_ticket(SESSION_A, generation, state.epoch)
        self.assertEqual(caught.exception.status, 503)


class EnrollmentFilesTest(unittest.TestCase):
    def setUp(self):
        self.config = desk_config(make_home(self))

    def test_enroll_unenroll_with_private_files_and_a_cap(self):
        enrollment = Enrollment(self.config.enrolled_file)
        self.assertTrue(enrollment.enroll(SESSION_A, 1.0))
        self.assertFalse(enrollment.enroll(SESSION_A, 2.0))
        self.assertEqual(mode(self.config.enrolled_file), 0o600)
        self.assertEqual(mode(self.config.config_dir), 0o700)
        self.assertEqual(mode(self.config.enrolled_file.with_suffix(".lock")), 0o600)
        self.assertEqual(json.loads(self.config.enrolled_file.read_text()), {"sessions": {SESSION_A: {"since": 1}}})
        self.assertTrue(enrollment.unenroll(SESSION_A))
        self.assertFalse(enrollment.unenroll(SESSION_A))
        for n in range(MAX_ENROLLED):
            enrollment.enroll(f"{n:08x}-0000-4000-8000-000000000000", 1.0)
        with self.assertRaises(DeskError):
            enrollment.enroll(SESSION_B, 1.0)
        with self.assertRaises(DeskError):
            enrollment.enroll("not-a-session", 1.0)

    def test_a_second_reader_sees_changes_and_corrupt_files_read_as_empty(self):
        reader = Enrollment(self.config.enrolled_file)
        self.assertFalse(reader.is_enrolled(SESSION_A))
        Enrollment(self.config.enrolled_file).enroll(SESSION_A, 1.0)
        self.assertTrue(reader.is_enrolled(SESSION_A))
        Enrollment(self.config.enrolled_file).unenroll(SESSION_A)
        self.assertFalse(reader.is_enrolled(SESSION_A))
        self.config.enrolled_file.write_text("{broken")
        with self.assertLogs("llm_switchboard.desk", "WARNING"):
            self.assertEqual(reader.sessions(), {})

    def test_sends_default_off_and_persist(self):
        settings = DeskSettings(self.config.settings_file)
        self.assertFalse(settings.sends_enabled())
        DeskSettings(self.config.settings_file).set_sends_enabled(True)
        self.assertTrue(settings.sends_enabled())
        self.assertEqual(mode(self.config.settings_file), 0o600)
        self.config.settings_file.write_text('{"sends_enabled": "yes"}')
        self.assertFalse(settings.sends_enabled())


if __name__ == "__main__":
    unittest.main()
