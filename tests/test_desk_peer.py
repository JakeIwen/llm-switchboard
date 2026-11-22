import dataclasses
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest

from llm_switchboard.desk.config import DeskConfig
from llm_switchboard.desk.peer import PeerIdentity, PeerRefused, recheck, socket_peer_pid, verify_peer

CHANNEL_PID, CLAUDE_PID, SESSION = 4001, 3001, "sess-aaa"
CHANNEL_START, CLAUDE_START = "900100", "800200"


class FakeSystem:
    """A fake /proc and ~/.claude/sessions under a temporary home."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="dp"))
        base = DeskConfig.from_env(home=self.root / "home", environ={})
        repo = self.root / "repo"
        self.python = repo / ".venv" / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("")
        self.other_exe = self.root / "other-exe"
        self.other_exe.write_text("")
        self.config = dataclasses.replace(base, repo=repo, proc_dir=self.root / "proc")
        self.config.claude_sessions_dir.mkdir(parents=True)
        self.enrolled = {SESSION}
        self.add_process(CHANNEL_PID, CLAUDE_PID, CHANNEL_START, self.config.channel_argv, self.python)
        self.add_process(CLAUDE_PID, 1, CLAUDE_START,
                         ("claude", *self.config.launch_flags[:2], "--dangerously-load-development-channels", "server:desk"),
                         self.root / "claude-bin")
        self.write_session(CLAUDE_PID, CLAUDE_START, SESSION)

    def add_process(self, pid, ppid, start, argv, exe):
        proc = self.config.proc_dir / str(pid)
        proc.mkdir(parents=True, exist_ok=True)
        fields = ["S", str(ppid)] + ["0"] * 17 + [start, "0"]
        # The command name holds spaces and ")" on purpose.
        (proc / "stat").write_text(f"{pid} (node (x) y) " + " ".join(fields) + "\n")
        (proc / "cmdline").write_bytes(b"".join(part.encode() + b"\0" for part in argv))
        link = proc / "exe"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(exe)

    def remove_process(self, pid):
        shutil.rmtree(self.config.proc_dir / str(pid))

    def write_session(self, pid, start, session_id):
        entry = {"pid": pid, "procStart": start, "sessionId": session_id}
        (self.config.claude_sessions_dir / f"{pid}.json").write_text(json.dumps(entry))

    def verify(self, session_id=SESSION, pid=CHANNEL_PID):
        return verify_peer(pid, session_id, self.config, lambda sid: sid in self.enrolled)

    def recheck(self, identity):
        return recheck(identity, self.config, lambda sid: sid in self.enrolled)


class VerifyPeerTest(unittest.TestCase):
    def setUp(self):
        self.system = FakeSystem()
        self.addCleanup(shutil.rmtree, self.system.root, True)

    def assert_refused(self, reason_part, **kwargs):
        with self.assertRaises(PeerRefused) as caught:
            self.system.verify(**kwargs)
        self.assertIn(reason_part, caught.exception.args[0])

    def test_accepted_launch(self):
        identity = self.system.verify()
        self.assertEqual(identity, PeerIdentity(CHANNEL_PID, CHANNEL_START, CLAUDE_PID, CLAUDE_START, SESSION))
        self.assertIsNone(self.system.recheck(identity))

    def test_wrong_cmdline(self):
        argv = (*self.system.config.channel_argv[:3], "/somewhere/else/desk-channel")
        self.system.add_process(CHANNEL_PID, CLAUDE_PID, CHANNEL_START, argv, self.system.python)
        self.assert_refused("not the desk channel server")

    def test_extra_argument(self):
        argv = (*self.system.config.channel_argv, "--extra")
        self.system.add_process(CHANNEL_PID, CLAUDE_PID, CHANNEL_START, argv, self.system.python)
        self.assert_refused("not the desk channel server")

    def test_wrong_exe(self):
        self.system.add_process(CHANNEL_PID, CLAUDE_PID, CHANNEL_START, self.system.config.channel_argv,
                                self.system.other_exe)
        self.assert_refused("executable")

    def test_exe_through_a_symlinked_interpreter_is_accepted(self):
        real = self.system.root / "real-python"
        real.write_text("")
        self.system.python.unlink()
        self.system.python.symlink_to(real)
        self.system.add_process(CHANNEL_PID, CLAUDE_PID, CHANNEL_START, self.system.config.channel_argv, real)
        self.assertEqual(self.system.verify().channel_pid, CHANNEL_PID)

    def test_decoy_second_child_of_the_same_claude(self):
        # Same claude, but a process that only looks like a node channel server.
        self.system.add_process(4002, CLAUDE_PID, "900300", ("node", "server.mjs"), self.system.other_exe)
        self.assert_refused("not the desk channel server", pid=4002)

    def test_decoy_with_the_right_argv_but_another_exe(self):
        self.system.add_process(4002, CLAUDE_PID, "900300", self.system.config.channel_argv, self.system.other_exe)
        self.assert_refused("executable", pid=4002)

    def test_peer_gone(self):
        self.assert_refused("gone", pid=9999)

    def test_parent_start_time_mismatch(self):
        self.system.write_session(CLAUDE_PID, "1", SESSION)
        self.assert_refused("earlier process")

    def test_session_file_names_another_pid(self):
        entry = {"pid": 1234, "procStart": CLAUDE_START, "sessionId": SESSION}
        (self.system.config.claude_sessions_dir / f"{CLAUDE_PID}.json").write_text(json.dumps(entry))
        self.assert_refused("another process")

    def test_session_file_for_another_session(self):
        self.system.write_session(CLAUDE_PID, CLAUDE_START, "sess-bbb")
        self.assert_refused("session id does not match")

    def test_hello_for_another_session(self):
        self.system.enrolled.add("sess-bbb")
        self.assert_refused("session id does not match", session_id="sess-bbb")

    def test_missing_session_file(self):
        (self.system.config.claude_sessions_dir / f"{CLAUDE_PID}.json").unlink()
        self.assert_refused("no session file")

    def test_parent_without_mcp_config(self):
        self.system.add_process(CLAUDE_PID, 1, CLAUDE_START, ("claude",), self.system.root / "claude-bin")
        self.assert_refused("pinned MCP config")

    def test_parent_with_another_mcp_config_path(self):
        argv = ("claude", "--mcp-config", "/tmp/evil/mcp.json", "--dangerously-load-development-channels", "server:desk")
        self.system.add_process(CLAUDE_PID, 1, CLAUDE_START, argv, self.system.root / "claude-bin")
        self.assert_refused("pinned MCP config")

    def test_mcp_config_flag_and_value_must_be_adjacent(self):
        argv = ("claude", "--mcp-config", "/tmp/evil/mcp.json", str(self.system.config.mcp_config))
        self.system.add_process(CLAUDE_PID, 1, CLAUDE_START, argv, self.system.root / "claude-bin")
        self.assert_refused("pinned MCP config")

    def test_not_enrolled(self):
        self.system.enrolled.clear()
        self.assert_refused("not enrolled")

    def test_parent_is_the_ppid_not_an_environment_claim(self):
        # A decoy claude with a valid session file is not the channel's parent.
        self.system.add_process(3002, 1, "800300", ("claude", "--mcp-config", str(self.system.config.mcp_config)),
                                self.system.root / "claude-bin")
        self.system.write_session(3002, "800300", "sess-other")
        self.system.enrolled.add("sess-other")
        self.assert_refused("session id does not match", session_id="sess-other")


class RecheckTest(unittest.TestCase):
    def setUp(self):
        self.system = FakeSystem()
        self.addCleanup(shutil.rmtree, self.system.root, True)
        self.identity = self.system.verify()

    def test_session_id_change_as_clear_does(self):
        self.system.write_session(CLAUDE_PID, CLAUDE_START, "sess-new")
        self.assertEqual(self.system.recheck(self.identity), "session id changed")

    def test_parent_dies(self):
        self.system.remove_process(CLAUDE_PID)
        self.assertIn("gone", self.system.recheck(self.identity))

    def test_parent_pid_reused_with_new_start_time(self):
        self.system.add_process(CLAUDE_PID, 1, "999999", ("claude",), self.system.root / "claude-bin")
        self.assertIn("gone", self.system.recheck(self.identity))

    def test_channel_dies(self):
        self.system.remove_process(CHANNEL_PID)
        self.assertIn("gone", self.system.recheck(self.identity))

    def test_channel_reparented(self):
        self.system.add_process(CHANNEL_PID, 1, CHANNEL_START, self.system.config.channel_argv, self.system.python)
        self.assertIn("reparented", self.system.recheck(self.identity))

    def test_session_file_removed(self):
        (self.system.config.claude_sessions_dir / f"{CLAUDE_PID}.json").unlink()
        self.assertIn("session file", self.system.recheck(self.identity))

    def test_unenrolled(self):
        self.system.enrolled.clear()
        self.assertEqual(self.system.recheck(self.identity), "session is not enrolled")


class SocketPeerPidTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "SO_PEERCRED peer checks need Linux")
    def test_unix_socketpair_reports_our_own_pid(self):
        left, right = socket.socketpair(socket.AF_UNIX)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        self.assertEqual(socket_peer_pid(left), os.getpid())

    @unittest.skipIf(sys.platform.startswith("linux"), "the refusal is for non-Linux platforms")
    def test_refuses_off_linux(self):
        left, right = socket.socketpair(socket.AF_UNIX)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        with self.assertRaises(PeerRefused) as caught:
            socket_peer_pid(left)
        self.assertIn("need Linux", caught.exception.args[0])


if __name__ == "__main__":
    unittest.main()
