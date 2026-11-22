"""The lock file and the enrollment/settings files: durability, modes, caps
and refusing to trust anything unreadable."""

import json
import os
import secrets
import stat
import unittest
from unittest import mock

from desk_fixtures import SESSION_A, SESSION_B, Clock, FakeIdentity, desk_config, make_home, request_id

from llm_switchboard.desk.enrollment import DeskSettings, Enrollment
from llm_switchboard.desk.hub_state import HubState
from llm_switchboard.desk.locks import LockStore, LockStoreError
from llm_switchboard.desk.model import MAX_ENROLLED, MAX_LOCK_FILE_BYTES, MAX_LOCKS, DeskError, Lock


def lock(n: int, session_id: str = SESSION_A) -> Lock:
    return Lock(f"{n:016x}", "pi", session_id, request_id(n), "ab" * 32, 1000.0 + n)


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

    def test_a_short_os_write_still_writes_the_whole_file(self):
        real_write = os.write

        def short_write(fd, data):
            return real_write(fd, bytes(data[:7]))
        store = LockStore(self.path)
        store.load()
        with mock.patch("llm_switchboard.desk.locks.os.write", side_effect=short_write):
            store.add(lock(1))
            store.add(lock(2, SESSION_B))
        self.assertEqual(LockStore(self.path).load(), [lock(1), lock(2, SESSION_B)])

    def test_a_write_that_makes_no_progress_fails_instead_of_looping(self):
        store = LockStore(self.path)
        store.load()
        with mock.patch("llm_switchboard.desk.locks.os.write", return_value=0):
            with self.assertRaisesRegex(LockStoreError, "unwritable"):
                store.add(lock(1))
        self.assertEqual(LockStore(self.path).load(), [])

    def test_an_oversize_file_is_refused_without_parsing_it(self):
        self.path.parent.mkdir(parents=True)
        padded = json.dumps({"version": 1, "locks": [lock(1).to_json()]}) + " " * MAX_LOCK_FILE_BYTES
        self.path.write_text(padded)
        with self.assertRaisesRegex(LockStoreError, "too large"):
            LockStore(self.path).load()

    def test_every_field_of_every_lock_is_validated_on_load(self):
        self.path.parent.mkdir(parents=True)
        good = lock(1).to_json()
        bad_fields = {
            "lock_id": ["", "x" * 16, "0" * 15, 7], "host": ["mac", "", None],
            "session_id": ["not-a-session", SESSION_A.upper(), 1],
            "request_id": ["epoch.1", "e" * 32 + ".zz", ["list"]], "ticket_hash": ["h", "A" * 64],
        }
        raw_since = ["NaN", "Infinity", "-Infinity", "-1", "0", "1e400", '"soon"', "true"]
        contents = [json.dumps({"version": 1, "locks": [good, {**lock(2).to_json(), name: value}]})
                    for name, values in bad_fields.items() for value in values]
        template = json.dumps({"version": 1, "locks": [{**good, "since": "SINCE"}]})
        contents += [template.replace('"SINCE"', since) for since in raw_since]
        contents += [json.dumps({"version": 1, "locks": [{**good, "extra": "text"}]}),
                     json.dumps({"version": 1, "locks": [{k: v for k, v in good.items() if k != "host"}]})]
        for content in contents:
            with self.subTest(content=content[-90:]):
                self.path.write_text(content)
                with self.assertRaises(LockStoreError):
                    LockStore(self.path).load()
        self.path.write_text(json.dumps({"version": 1, "locks": [good]}))
        self.assertEqual(LockStore(self.path).load(), [lock(1)])

    def test_a_malformed_lock_is_never_written(self):
        store = LockStore(self.path)
        store.load()
        with self.assertRaisesRegex(LockStoreError, "malformed"):
            store.add(Lock("0" * 16, "pi", SESSION_A, "not an id", "ab" * 32, 1.0))
        self.assertFalse(self.path.exists())

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

    def test_edits_refuse_an_unreadable_or_malformed_enrollment_file(self):
        enrollment = Enrollment(self.config.enrolled_file)
        enrollment.enroll(SESSION_A, 1.0)
        for content in ("{broken", "[1, 2]", '{"sessions": []}', '{"sessions": "x"}'):
            with self.subTest(content=content):
                self.config.enrolled_file.write_text(content)
                for edit in (lambda: enrollment.enroll(SESSION_B, 2.0), lambda: enrollment.unenroll(SESSION_A)):
                    with self.assertRaises(DeskError) as caught:
                        edit()
                    self.assertEqual(caught.exception.status, 503)
                    self.assertEqual(self.config.enrolled_file.read_text(), content)
        # Routing still fails closed: an unreadable file means nobody enrolled.
        with self.assertLogs("llm_switchboard.desk", "WARNING"):
            self.config.enrolled_file.write_text("{broken!")
            self.assertEqual(enrollment.sessions(), {})

    def test_sends_replace_a_broken_settings_file(self):
        self.config.config_dir.mkdir(parents=True)
        self.config.settings_file.write_text("{broken")
        with self.assertLogs("llm_switchboard.desk", "WARNING"):
            DeskSettings(self.config.settings_file).set_sends_enabled(True)
        self.assertEqual(json.loads(self.config.settings_file.read_text()), {"sends_enabled": True})

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
