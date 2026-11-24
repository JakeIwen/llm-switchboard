"""The lock file and the enrollment/settings files: durability, modes, caps
and refusing to trust anything unreadable."""

import json
import os
from pathlib import Path
import pwd
import secrets
import select
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from desk_fixtures import (
    REPO, SESSION_A, SESSION_B, Clock, FakeIdentity, HubCase, desk_config, make_home, request_id, run_desk_cli,
)

from llm_switchboard.desk.enrollment import DeskSettings, Enrollment
from llm_switchboard.desk.config import DeskConfig
from llm_switchboard.json_file import JsonFile
from llm_switchboard.desk.hub_state import HubState
from llm_switchboard.desk.locks import LockStore, LockStoreError
from llm_switchboard.desk.model import MAX_ENROLLED, MAX_LOCK_FILE_BYTES, MAX_LOCKS, DeskError, Lock


def lock(n: int, session_id: str = SESSION_A) -> Lock:
    return Lock(f"{n:016x}", "pi", session_id, request_id(n), "ab" * 32, 1000.0 + n, 4000 + n, str(900 + n))


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
            "channel_pid": [0, -1, 2**31, True, "4001", 1.5, None],
            "channel_start": ["", "12a", " 12", "1" * 21, 12, None],
        }
        raw_since = ["NaN", "Infinity", "-Infinity", "-1", "0", "1e400", '"soon"', "true"]
        contents = [json.dumps({"version": 1, "locks": [good, {**lock(2).to_json(), name: value}]})
                    for name, values in bad_fields.items() for value in values]
        template = json.dumps({"version": 1, "locks": [{**good, "since": "SINCE"}]})
        contents += [template.replace('"SINCE"', since) for since in raw_since]
        contents += [json.dumps({"version": 1, "locks": [{**good, "extra": "text"}]}),
                     json.dumps({"version": 1, "locks": [{k: v for k, v in good.items() if k != "host"}]})]
        # The writer's identity is required: a lock without it could never be cleared safely.
        contents += [json.dumps({"version": 1, "locks": [{k: v for k, v in good.items() if k != name}]})
                     for name in ("channel_pid", "channel_start")]
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
            store.add(Lock("0" * 16, "pi", SESSION_A, "not an id", "ab" * 32, 1.0, 4001, "900"))
        with self.assertRaisesRegex(LockStoreError, "bad channel_pid"):
            store.add(Lock("0" * 16, "pi", SESSION_A, request_id(1), "ab" * 32, 1.0, 0, "900"))
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
        state = HubState(LockStore(self.path), now=Clock(), elapsed=Clock(), token_bytes=secrets.token_bytes)
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

    def test_a_bad_record_makes_the_whole_enrollment_file_malformed(self):
        enrollment = Enrollment(self.config.enrolled_file)
        enrollment.enroll(SESSION_A, 1.0)
        bad = [{SESSION_A: None}, {SESSION_A: {"since": "x"}}, {SESSION_A: {"since": True}},
               {SESSION_A: {"since": 1, "note": "x"}}, {SESSION_A: {}}, {"not-a-session": {"since": 1}},
               {SESSION_A: {"since": 1}, SESSION_B: None}]
        contents = [json.dumps({"sessions": sessions}) for sessions in bad]
        contents.append(json.dumps({"sessions": {SESSION_A: {"since": 1}}, "other": 1}))
        for content in contents:
            with self.subTest(content=content):
                # A good read in between: a file that stays broken warns once.
                self.config.enrolled_file.write_text(json.dumps({"sessions": {SESSION_A: {"since": 1}}}))
                self.assertTrue(enrollment.is_enrolled(SESSION_A))
                self.config.enrolled_file.write_text(content)
                with self.assertLogs("llm_switchboard.desk", "WARNING"):
                    self.assertFalse(enrollment.is_enrolled(SESSION_A))
                for edit in (lambda: enrollment.enroll(SESSION_B, 2.0), lambda: enrollment.unenroll(SESSION_A)):
                    with self.assertRaises(DeskError) as caught:
                        edit()
                    self.assertEqual(caught.exception.status, 503)
                    self.assertEqual(self.config.enrolled_file.read_text(), content)

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


# An offline `desk unenroll` paused inside its read-modify-write: it holds
# enrolled.json's lock (as Enrollment.unenroll takes it) until told to go on.
_PAUSED_OFFLINE_UNENROLL = """
import sys
from pathlib import Path
from llm_switchboard.desk.enrollment import Enrollment
enrollment = Enrollment(Path(sys.argv[1]))
with enrollment.locked():
    print("locked", flush=True)
    sys.stdin.readline()
    enrollment.unenroll(sys.argv[2])
"""


# An offline `desk unenroll` that starts its edit when told to.
_OFFLINE_UNENROLL_ON_CUE = """
import sys
from pathlib import Path
from llm_switchboard.desk.enrollment import Enrollment
enrollment = Enrollment(Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.readline()
with enrollment.locked():
    print("locked", flush=True)
    enrollment.unenroll(sys.argv[2])
"""


_MARKER_TIMEOUT_SECONDS = 10


def read_marker(process, timeout: float = _MARKER_TIMEOUT_SECONDS) -> str:
    """The child's next stdout line, read unbuffered from the pipe; fails the
    test (so its cleanup kills the child) instead of hanging on a deadlock."""
    fd, line, deadline = process.stdout.fileno(), b"", time.monotonic() + timeout
    while not line.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            raise AssertionError(f"no marker from the child within {timeout} s (got {line!r})")
        chunk = os.read(fd, 1)
        if not chunk:
            raise AssertionError(f"the child closed stdout (got {line!r})")
        line += chunk
    return line.decode().strip()


class SocketPathTest(unittest.TestCase):
    def test_the_operator_socket_ignores_xdg_runtime_dir_for_the_account_home(self):
        # The one HOME for which the runtime dir could follow XDG_RUNTIME_DIR;
        # nothing is created, only paths compared.
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        plain = DeskConfig.from_env(environ={"HOME": str(home)})
        with_runtime = DeskConfig.from_env(environ={"HOME": str(home), "XDG_RUNTIME_DIR": "/run/user/elsewhere"})
        self.assertEqual(with_runtime.operator_socket, plain.operator_socket)
        self.assertEqual(with_runtime.channel_socket, plain.channel_socket)


class PolicyFileLockTest(HubCase):
    """The hub and the CLI's offline edits share the policy files' locks."""

    def test_a_cli_with_another_runtime_dir_finds_the_running_hub(self):
        other_runtime = make_home(self)
        done = run_desk_cli(self.home, "unenroll", SESSION_A, env={"XDG_RUNTIME_DIR": str(other_runtime)})
        self.assertEqual(done.returncode, 0, done.stderr)
        # Through the hub, not an offline edit: its state changed without a recheck.
        self.assertNotIn(SESSION_A, self.hub.state.target_session_ids())

    def test_an_offline_edit_holding_the_file_lock_holds_a_handoff_until_it_is_written(self):
        client = self.channel(SESSION_A)
        generation = client.first[1]["generation"]
        ticket = self.hub.ticket({"session_id": SESSION_A, "generation": generation, "epoch": self.hub.epoch},
                                 self.token)["ticket"]
        editor = subprocess.Popen([sys.executable, "-c", _PAUSED_OFFLINE_UNENROLL, str(self.config.enrolled_file),
                                   SESSION_A], cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(editor.wait)
        self.addCleanup(editor.kill)
        self.assertEqual(read_marker(editor), "locked")
        outcome = {}

        def send():
            try:
                outcome["sent"] = self.hub.send({"ticket": ticket, "text": "hi"}, self.token)
            except DeskError as exc:
                outcome["refused"] = (exc.status, exc.reason)

        sender = threading.Thread(target=send)
        sender.start()
        # A bounded wait for something that must not happen: the handoff waits on the lock.
        sender.join(0.5)
        self.assertTrue(sender.is_alive())
        self.assertEqual([r["state"] for r in self.hub.state_view()["requests"]], ["pending"])
        editor.stdin.write("go\n")
        editor.stdin.flush()
        self.assertEqual(editor.wait(timeout=10), 0)
        sender.join(10)
        # The handoff reread the file after the edit landed.
        self.assertEqual(outcome, {"refused": (409, "not enrolled")})
        self.assertEqual([r["state"] for r in self.hub.state_view()["requests"]], ["expired"])
        # Nothing was written to the channel.
        client.sock.settimeout(0.2)
        with self.assertRaises(TimeoutError):
            client.read()

    def test_an_offline_edit_cannot_land_between_the_reread_and_the_target_lock(self):
        client = self.channel(SESSION_A)
        generation = client.first[1]["generation"]
        ticket = self.hub.ticket({"session_id": SESSION_A, "generation": generation, "epoch": self.hub.epoch},
                                 self.token)["ticket"]
        editor = subprocess.Popen([sys.executable, "-c", _OFFLINE_UNENROLL_ON_CUE, str(self.config.enrolled_file),
                                   SESSION_A], cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(editor.wait)
        self.addCleanup(editor.kill)
        self.assertEqual(read_marker(editor), "ready")
        real_hand_off, seen = self.hub.state.hand_off, {}

        def hand_off_while_an_edit_tries(request_id):
            # The files were just reread; the edit starts now and must wait.
            editor.stdin.write("go\n")
            editor.stdin.flush()
            ready, _, _ = select.select([editor.stdout], [], [], 0.5)
            seen["edit got in"] = bool(ready)
            return real_hand_off(request_id)

        with mock.patch.object(self.hub.state, "hand_off", hand_off_while_an_edit_tries):
            sent = self.hub.send({"ticket": ticket, "text": "hi"}, self.token)
        self.assertEqual((seen, sent["state"]), ({"edit got in": False}, "handed_off"))
        # The edit lands once the target lock is persisted.
        self.assertEqual(read_marker(editor), "locked")
        self.assertEqual(editor.wait(timeout=10), 0)
        self.assertFalse(Enrollment(self.config.enrolled_file).is_enrolled(SESSION_A))


class OfflineEditLockTest(unittest.TestCase):
    def test_the_offline_cli_edit_waits_for_the_files_lock(self):
        home = make_home(self)
        config = desk_config(home)
        Enrollment(config.enrolled_file).enroll(SESSION_A, 1.0)
        before = config.enrolled_file.read_text()
        # Held through a separate open of the lock file, as another process would.
        with JsonFile(config.enrolled_file).locked():
            cli = subprocess.Popen([sys.executable, "-m", "llm_switchboard", "desk", "unenroll", SESSION_A],
                                   cwd=REPO, env={**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": "",
                                                  "XDG_STATE_HOME": "", "XDG_RUNTIME_DIR": "",
                                                  "LLM_SWITCHBOARD_DESK_PORT": ""},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(cli.wait)
            self.addCleanup(cli.kill)
            # A bounded wait for something that must not happen: the edit waits on the lock.
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and cli.poll() is None:
                time.sleep(0.05)
            self.assertIsNone(cli.poll())
            self.assertEqual(config.enrolled_file.read_text(), before)
        out, err = cli.communicate(timeout=30)
        self.assertEqual(cli.returncode, 0, err)
        self.assertFalse(Enrollment(config.enrolled_file).is_enrolled(SESSION_A))


if __name__ == "__main__":
    unittest.main()
