"""Mac identity checks against an isolated registry and a replaceable kernel reader."""

from dataclasses import replace
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import socket
import sys
import unittest

from desk_fixtures import SESSION_A, SESSION_B, desk_config, make_home
from llm_switchboard.desk import peer_mac as peer
from llm_switchboard.desk.mac_process import LibprocReader, MacProcess, ProcessStamp
from llm_switchboard.desk.model import ChannelProcess


class FakeKernel:
    def __init__(self, records):
        self.records = {p.stamp.pid: p for p in records}
        self.cred = (records[0].stamp.pid, os.getuid())
        self.failure = None

    def credentials(self, sock):
        return self.cred

    def process(self, pid):
        if self.failure:
            raise self.failure
        return self.records[pid]

    def stamp(self, pid):
        return self.process(pid).stamp


class IdentityTests(unittest.TestCase):
    def setUp(self):
        home = make_home(self)
        self.config = replace(desk_config(home), repo=home / "checkout")
        c = self.config
        self.interpreter = home / "Python.framework/Versions/3.13/bin/python3.13"
        self.interpreter.parent.mkdir(parents=True)
        self.interpreter.touch()
        pinned = Path(c.channel_argv[0])
        pinned.parent.mkdir(parents=True)
        pinned.symlink_to(self.interpreter)
        self.app = str(self.interpreter.resolve().parent.parent / "Resources/Python.app/Contents/MacOS/Python")
        c.claude_versions_dir.mkdir(parents=True)
        installed = c.claude_versions_dir.resolve() / "2.1.330"
        installed.touch()
        self.sec = int(datetime(2026, 11, 23, 2, 0, 40, tzinfo=timezone.utc).timestamp())
        self.parent = MacProcess(ProcessStamp(42, 10, os.getuid(), self.sec, 444), 123, 12,
                                 str(installed), "/Users/me/bin/claude", ("claude", *c.launch_flags))
        self.child = MacProcess(ProcessStamp(43, 42, os.getuid(), self.sec + 1, 0), 456, 123,
                                self.app, self.app, (self.app, *c.channel_argv[1:]))
        self.kernel = FakeKernel([self.child, self.parent])
        c.claude_sessions_dir.mkdir(parents=True)
        self.registry = c.claude_sessions_dir / "42.json"
        self.entry = {"sessionId": SESSION_A, "pid": 42, "procStart": "Mon Nov 23 02:00:40 2026"}
        self.publish()

    def publish(self, **changes):
        self.registry.write_text(json.dumps(self.entry | changes))
        ns = self.parent.stamp.micros * 1000
        os.utime(self.registry, ns=(ns, ns))

    def verify(self):
        pid = peer.socket_peer_pid(None, reader=self.kernel)
        return peer.verify_peer(pid, SESSION_A, self.config, reader=self.kernel)

    def refused(self):
        with self.assertRaises(peer.PeerRefused):
            self.verify()

    def test_other_uid_channel_refused(self):
        self.kernel.cred = 43, os.getuid() + 1
        self.refused()
        self.kernel.cred = 43, os.getuid()
        self.kernel.records[43] = replace(self.child, stamp=replace(self.child.stamp, uid=os.getuid() + 1))
        self.refused()

    def test_parent_not_installed_claude_refused(self):
        root = self.config.claude_versions_dir
        for exe in [root.parent / "2.1", root / "sub/2.1", root / "claude", root / "2.1.4"]:
            with self.subTest(exe=exe):
                self.kernel.records[42] = replace(self.parent, exe=str(exe))
                self.refused()
        alias = root / "2.1.1"
        alias.symlink_to(self.parent.exe)
        self.kernel.records[42] = replace(self.parent, exe=str(alias))
        self.refused()

    def test_desk_flags_not_first_refused(self):
        for before in [("--",), ("--name",), ("--verbose",)]:
            self.kernel.records[42] = replace(self.parent, argv=("claude", *before, *self.config.launch_flags))
            self.refused()

    def test_stale_session_file_same_second_refused(self):
        self.assertEqual(self.verify().session_id, SESSION_A)
        ns = self.parent.stamp.micros * 1000 - 1000
        os.utime(self.registry, ns=(ns, ns))
        self.refused()

    def test_pinned_interpreter_paths_and_complete_tail(self):
        for exe in (self.app, str(self.interpreter.resolve())):
            good = replace(self.child, exe=exe, exec_path=exe, argv=(exe, *self.config.channel_argv[1:]))
            self.kernel.records[43] = good
            self.assertEqual(self.verify().channel_unique, self.child.unique)
            for bad in [replace(good, argv=("python", *good.argv[1:])), replace(good, exe=exe+"x"),
                        replace(good, exec_path=exe+"x"), replace(good, argv=good.argv[:-1]),
                        replace(good, argv=good.argv + ("extra",))]:
                self.kernel.records[43] = bad
                self.refused()
        pin = Path(self.config.channel_argv[0])
        pin.unlink()
        pin.symlink_to(self.interpreter.parent / "python3.14")
        self.kernel.records[43] = self.child
        self.refused()

    def test_decoy_python_child_and_parent_pid_reuse(self):
        decoy = replace(self.child, stamp=replace(self.child.stamp, pid=55),
                        argv=(self.app, "decoy.py"), unique=987)
        self.kernel.records[55] = decoy
        self.kernel.cred = 55, os.getuid()
        self.refused()  # Another genuine child of that same parent cannot vouch for the peer.
        self.kernel.cred = 43, os.getuid()
        self.kernel.records[42] = replace(self.parent, unique=self.parent.unique + 1)
        self.refused()

    def test_proc_start_utc_one_or_two_digit_day_either_spacing(self):
        for day in (3, 23):
            date = datetime(2026, 11, day, 2, 0, 40, tzinfo=timezone.utc)
            self.parent = replace(self.parent, stamp=replace(self.parent.stamp, sec=int(date.timestamp())))
            self.kernel.records[42] = self.parent
            for gap in (" ", "  "):
                self.publish(procStart=f"{date:%a %b}{gap}{day} {date:%H:%M:%S %Y}")
                self.assertEqual(self.verify().claude_start, str(self.parent.stamp.micros))

    def test_wrong_session_file_fields_and_unreadable_files_refused(self):
        for values in ({"pid": 3}, {"pid": 42.0}, {"sessionId": SESSION_B}, {"procStart": self.sec},
                       {"procStart": "Mon Nov 23 02:00:39 2026"}, {"procStart": "bad"}):
            self.publish(**values)
            self.refused()
        for data in (b"[" * 10000 + b"]" * 10000, b"x" * 65537, b"\xff"):
            self.registry.write_bytes(data)
            self.refused()
        self.publish()
        target = self.registry.with_suffix(".other")
        self.registry.rename(target)
        self.registry.symlink_to(target)
        self.refused()
        self.registry.unlink()
        os.mkfifo(self.registry)
        self.refused()

    def test_recheck_repeats_launch_and_kernel_identities(self):
        identity = self.verify()
        self.assertIsNone(peer.recheck(identity, self.config, reader=self.kernel))
        for pid, updated in [(43, replace(self.child, unique=789)),
                             (43, replace(self.child, stamp=replace(self.child.stamp, usec=1))),
                             (42, replace(self.parent, unique=1234)), (42, replace(self.parent, argv=()))]:
            self.kernel.records[pid] = updated
            self.assertIsNotNone(peer.recheck(identity, self.config, reader=self.kernel))
            self.kernel.records[pid] = self.child if pid == 43 else self.parent
        self.publish(sessionId=SESSION_B)
        self.assertIsNotNone(peer.recheck(identity, self.config, reader=self.kernel))

    def test_presence_only_a_definitive_loss_is_gone(self):
        ident = self.verify()
        def presence(start):
            return peer.process_presence(ident.channel_pid, start, reader=self.kernel)
        self.assertEqual(presence(ident.channel_start), ChannelProcess.RUNNING)
        self.assertEqual(presence(str(int(ident.channel_start) + 1)), ChannelProcess.GONE)
        for error in (errno.EPERM, errno.EIO, errno.ENOENT, errno.ESRCH):
            self.kernel.failure = OSError(error, "unreadable")
            self.assertEqual(presence(ident.channel_start), ChannelProcess.GONE if error == errno.ESRCH
                             else ChannelProcess.UNVERIFIABLE)


@unittest.skipUnless(sys.platform == "darwin", "libproc and LOCAL_PEERPID are Darwin kernel APIs")
class NativeReaderTests(unittest.TestCase):
    def test_reads_this_interpreter_and_unix_socket_credentials(self):
        reader = LibprocReader()
        me = reader.process(os.getpid())
        self.assertEqual((me.stamp.pid, me.stamp.ppid, me.stamp.uid), (os.getpid(), os.getppid(), os.getuid()))
        self.assertGreater(me.unique, 0)
        self.assertGreater(me.parent_unique, 0)
        self.assertEqual(me.argv[1:], tuple(sys.orig_argv[1:]))
        self.assertTrue(peer.runs_pinned(me, (sys.executable, *me.argv[1:])))
        path = str(make_home(self) / "s")
        with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
            server.bind(path)
            server.listen(1)
            client.connect(path)
            conn, _ = server.accept()
            with conn:
                self.assertEqual(peer.socket_peer_pid(conn, reader=reader), os.getpid())
