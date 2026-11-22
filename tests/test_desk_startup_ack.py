"""The startup acknowledgement: the protected view of startup-blocked
sessions, the one-Enter mutation over HTTPS and `desk ack`, against a real
isolated tmux server whose pane runs a fake Claude warning screen.

Process identity is read from a fake process tree (`config.proc_dir`) that
mirrors the real pane pid, so the cases run on any OS; one Linux-only case
reads the real /proc of a real `bash -ic` -> `claude` chain."""

import dataclasses
import hashlib
import itertools
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from desk_fixtures import SESSION_A, SESSION_B, SESSION_C, Clock, HubCase, run_desk_cli, wait_until

from llm_switchboard.desk.model import MAX_STARTUP_ACKS_PER_MINUTE
from llm_switchboard.desk.operator import OperatorOp, operator_request
from llm_switchboard.desk.startup_identity import StartupRefusal, check_launch_argv, refused
from llm_switchboard.desk.startup_registry import MAX_SESSION_FILE_BYTES
from llm_switchboard.paths import MANAGED_ENV_VAR
from llm_switchboard.screen import desk_startup_warning
from llm_switchboard.tmux import Tmux, TmuxError

FAKE_PANE = Path(__file__).parent / "fake_claude_pane.py"
_sockets = itertools.count(1)
# Fictitious pids for the fake tree's claude processes; only the pane pid is real.
_claude_pids = itertools.count(3_900_001)


class RecordingTmux(Tmux):
    """The real tmux, plus hooks run just before a capture or the guarded
    send (to change things mid-acknowledgement), one run just after the send,
    and a log of every key-sending argv."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.before_capture: list = []
        self.before_send: list = []
        self.after_send: list = []
        self.sent: list[list[str]] = []
        # The next this many capture-pane calls fail the way tmux reports an
        # error (exit status 1), for a pane that still exists.
        self.failing_captures = 0

    def capture(self, target, history=0, check=False):
        if self.before_capture:
            self.before_capture.pop(0)()
        return super().capture(target, history, check=check)

    def send_enter_if(self, pane_id, guard):
        if self.before_send:
            self.before_send.pop(0)()
        outcome = super().send_enter_if(pane_id, guard)
        if self.after_send:
            self.after_send.pop(0)()
        return outcome

    def _run(self, args, check=True, new_server=False):
        if args and args[0] in ("send-keys", "if-shell"):
            self.sent.append(list(args))
        if args and args[0] == "capture-pane" and self.failing_captures:
            self.failing_captures -= 1
            if check:
                raise TmuxError("capture-pane failed: simulated")
            return subprocess.CompletedProcess(args, 1, "", "simulated")
        return super()._run(args, check=check, new_server=new_server)


@dataclasses.dataclass
class FakeLaunch:
    session_id: str
    window_id: str
    pane_id: str
    pane_pid: int
    claude_pid: int
    state: Path

    def typed(self) -> bytes:
        path = self.state / "typed"
        return path.read_bytes() if path.exists() else b""

    def program_pid(self) -> int:
        return int((self.state / "pid").read_text())


class StartupCase(HubCase):
    # The acknowledgement sends no prompt, so it never depends on sends.
    sends_enabled = False

    def setUp(self):
        self.tmux = RecordingTmux(socket=f"desk-ack-{os.getpid()}-{next(_sockets)}", timeout=5)
        # tmux can leave its socket file behind after kill-server.
        self.socket_file = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / self.tmux.socket
        self.addCleanup(self.socket_file.unlink, missing_ok=True)
        self.addCleanup(self.tmux.kill_server)
        super().setUp()
        self.config.claude_sessions_dir.mkdir(parents=True, exist_ok=True)
        # The native installer's layout: claude's executables, one per version.
        self.config.claude_versions_dir.mkdir(parents=True)
        self.claude_exe = self.config.claude_versions_dir / "2.1.330"
        self.claude_exe.write_bytes(b"\x7fELF")

    def start_hub(self, **options):
        options.setdefault("tmux", self.tmux)
        # The rate windows read this clock; a test moves it past a full window.
        self.clock = Clock()
        options.setdefault("now", self.clock)
        return super().start_hub(**options)

    # -- the fake pane and its process tree ----------------------------------

    def claude_argv(self, session_id, chooser="--resume"):
        return ("claude", "--permission-mode", "bypassPermissions", chooser, session_id,
                "--remote-control", "desk test", *self.config.launch_flags)

    def launch(self, session_id=SESSION_A, *, tag=None, argv=None, managed=None, write_tree=True,
               exe=None) -> FakeLaunch:
        state = Path(tempfile.mkdtemp(prefix="pane", dir=self.home))
        command = f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_PANE))} {shlex.quote(str(state))}"
        tag = tag or session_id
        window_id = self.tmux.start(tag, f"fake-{tag[:4]}", command, {MANAGED_ENV_VAR: tag})
        wait_until(lambda: (state / "pid").exists())
        pane = next(p for p in self.tmux.panes() if p.window_id == window_id)
        wait_until(lambda: desk_startup_warning(self.tmux.capture(pane.pane_id)))
        launch = FakeLaunch(session_id, window_id, pane.pane_id, pane.pane_pid, next(_claude_pids), state)
        if write_tree:
            self.write_tree(launch, argv or self.claude_argv(session_id), managed or tag, exe=exe)
        return launch

    def write_tree(self, launch: FakeLaunch, argv, managed, *, start="777", exe=None):
        """The pane program as a `bash -ic`, its claude child leading the
        terminal's foreground group and running an installed claude, as on
        the Pi."""
        self.write_process(launch.pane_pid, 1, "100", ("bash", "-lic", "claudepd"),
                           pgrp=launch.pane_pid, tpgid=launch.claude_pid)
        self.write_process(launch.claude_pid, launch.pane_pid, start, argv, pgrp=launch.claude_pid,
                           tpgid=launch.claude_pid, environ={MANAGED_ENV_VAR: managed}, exe=exe or self.claude_exe)

    def write_process(self, pid, ppid, start, argv, *, pgrp, tpgid, environ=None, exe=None):
        entry = self.config.proc_dir / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        fields = ["S", str(ppid), str(pgrp), "1", "34816", str(tpgid)] + ["0"] * 13 + [start, "0"]
        (entry / "stat").write_text(f"{pid} (x) " + " ".join(fields) + "\n")
        (entry / "cmdline").write_bytes(b"".join(arg.encode() + b"\0" for arg in argv))
        (entry / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in (environ or {}).items()))
        (entry / "exe").unlink(missing_ok=True)
        if exe is not None:
            (entry / "exe").symlink_to(exe)

    # -- the API -------------------------------------------------------------

    def view(self, token=None):
        status, _, body = self.call("GET", "/desk/api/startup", token=token or self.token)
        self.assertEqual(status, 200, body)
        return {item["session_id"]: item for item in body["sessions"]}

    def ack(self, session_id, digest, token=None):
        status, _, body = self.call("POST", "/desk/api/startup/ack", {"session_id": session_id, "digest": digest},
                                    token=token or self.token)
        return status, body

    def assertAckRefused(self, launch, digest, refusal, *, token=None, keys_issued=0):
        """Refused with the refusal's own status, and nothing typed. A guard
        refusal (`keys_issued=1`) reaches tmux, whose guard then types nothing."""
        got = self.ack(launch.session_id, digest, token=token)
        self.assertEqual(got, (refused(refusal).status, {"error": refusal.value}))
        self.assertEqual(launch.typed(), b"")
        self.assertEqual(len(self.tmux.sent), keys_issued)

    def assertOneGuardedEnter(self, launch):
        """Exactly one key command reached tmux: Enter in this pane, behind the guard."""
        self.assertEqual(len(self.tmux.sent), 1, self.tmux.sent)
        command = self.tmux.sent[0]
        self.assertEqual(command[:4], ["if-shell", "-t", launch.pane_id, "-F"])
        self.assertEqual(command[5], f"send-keys -t {launch.pane_id} Enter ; display-message -p desk-enter-sent")

    def digest_for(self, launch):
        item = self.view()[launch.session_id]
        self.assertIsNone(item["refusal"])
        return item["digest"]


class AcknowledgeTest(StartupCase):
    def test_one_enter_and_nothing_else_when_everything_matches(self):
        launch = self.launch()
        item = self.view()[SESSION_A]
        self.assertTrue(desk_startup_warning(item["capture"]))
        self.assertRegex(item["digest"], "^[0-9a-f]{64}$")
        self.assertEqual(self.ack(SESSION_A, item["digest"]), (200, {"session_id": SESSION_A, "sent": True}))
        wait_until(lambda: launch.typed())
        self.assertEqual(launch.typed(), b"\r")
        self.assertOneGuardedEnter(launch)
        wait_until(lambda: "accepted" in self.tmux.capture(launch.pane_id))
        self.assertEqual(self.view(), {})
        # The same digest again: the screen moved on, and nothing more is typed.
        self.assertEqual(self.ack(SESSION_A, item["digest"]), (409, {"error": StartupRefusal.NOT_BLOCKED.value}))
        self.assertEqual(launch.typed(), b"\r")

    def test_a_new_session_launched_with_session_id_is_acknowledged_too(self):
        launch = self.launch(argv=self.claude_argv(SESSION_A, "--session-id"))
        self.assertEqual(self.ack(SESSION_A, self.digest_for(launch))[0], 200)
        wait_until(lambda: launch.typed() == b"\r")

    def test_a_session_file_naming_the_same_session_is_accepted(self):
        launch = self.launch()
        (self.config.claude_sessions_dir / f"{launch.claude_pid}.json").write_text(
            json.dumps({"pid": launch.claude_pid, "sessionId": SESSION_A}))
        self.assertEqual(self.ack(SESSION_A, self.digest_for(launch))[0], 200)

    def test_the_operator_cli_shows_the_screen_then_sends_with_its_digest(self):
        launch = self.launch()
        shown = run_desk_cli(self.home, "ack", SESSION_A)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("WARNING: Loading development channels", shown.stdout)
        self.assertIn("nothing sent", shown.stdout)
        digest = shown.stdout.split("digest ", 1)[1].split()[0]
        self.assertEqual(launch.typed(), b"")
        wrong = run_desk_cli(self.home, "ack", SESSION_A, "--digest", "0" * 64)
        self.assertEqual((wrong.returncode, wrong.stderr.strip()), (1, f"desk: {StartupRefusal.DIGEST_MISMATCH.value}"))
        sent = run_desk_cli(self.home, "ack", SESSION_A, "--digest", digest)
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn("Enter sent", sent.stdout)
        wait_until(lambda: launch.typed())
        self.assertEqual(launch.typed(), b"\r")

    def test_the_cli_needs_the_hub(self):
        self.hub.close()
        result = run_desk_cli(self.home, "ack", SESSION_A, "--digest", "0" * 64)
        self.assertEqual(result.returncode, 3, result.stderr)

    def test_acknowledgements_are_rate_limited(self):
        self.launch()
        for _ in range(MAX_STARTUP_ACKS_PER_MINUTE):
            self.assertEqual(self.ack(SESSION_A, "0" * 64)[0], 409)
        self.assertEqual(self.ack(SESSION_A, "0" * 64), (429, {"error": StartupRefusal.TOO_MANY.value}))

    def test_one_view_gets_one_enter_even_while_the_screen_stays(self):
        launch = self.launch()
        # Claude hasn't processed the key yet: the same warning stays on screen.
        (launch.state / "ignore-enter").touch()
        digest = self.digest_for(launch)
        self.assertEqual(self.ack(SESSION_A, digest)[0], 200)
        wait_until(lambda: launch.typed() == b"\r")
        item = self.view()[SESSION_A]
        self.assertEqual((item["digest"], item["refusal"]), (None, StartupRefusal.ALREADY_SENT.value))
        self.assertEqual(self.ack(SESSION_A, digest), (409, {"error": StartupRefusal.ALREADY_SENT.value}))
        cli = run_desk_cli(self.home, "ack", SESSION_A, "--digest", digest)
        self.assertEqual((cli.returncode, cli.stderr.strip()), (1, f"desk: {StartupRefusal.ALREADY_SENT.value}"))
        self.assertEqual(launch.typed(), b"\r")
        self.assertOneGuardedEnter(launch)
        # A different view (here, the screen redrawn) is a fresh one.
        os.kill(launch.program_pid(), signal.SIGUSR2)
        wait_until(lambda: self.view()[SESSION_A]["digest"] not in (None, digest))

    def test_an_unconfirmed_enter_is_reported_unknown_and_still_counts(self):
        launch = self.launch()
        (launch.state / "ignore-enter").touch()
        digest = self.digest_for(launch)

        def timed_out():
            raise subprocess.TimeoutExpired(["tmux"], 3)
        self.tmux.after_send = [timed_out]
        self.assertEqual(self.ack(SESSION_A, digest),
                         (503, {"error": StartupRefusal.DELIVERY_UNKNOWN.value, "delivery": "unknown"}))
        # Attempted, not sent: nothing claims the Enter arrived.
        self.assertEqual(self.ack(SESSION_A, digest), (409, {"error": StartupRefusal.ALREADY_ATTEMPTED.value}))
        self.assertEqual(self.view()[SESSION_A]["refusal"], StartupRefusal.ALREADY_ATTEMPTED.value)
        wait_until(lambda: launch.typed() == b"\r")
        self.assertOneGuardedEnter(launch)

    def test_the_cli_exits_4_when_the_enter_is_unconfirmed(self):
        launch = self.launch()
        shown = run_desk_cli(self.home, "ack", SESSION_A)
        digest = shown.stdout.split("digest ", 1)[1].split()[0]

        def timed_out():
            raise subprocess.TimeoutExpired(["tmux"], 3)
        self.tmux.after_send = [timed_out]
        result = run_desk_cli(self.home, "ack", SESSION_A, "--digest", digest)
        self.assertEqual((result.returncode, result.stderr.strip()), (4, f"desk: {StartupRefusal.DELIVERY_UNKNOWN.value}"))
        wait_until(lambda: launch.typed() == b"\r")

    def test_a_digest_from_before_a_hub_restart_is_refused(self):
        launch = self.launch()
        (launch.state / "ignore-enter").touch()
        digest = self.digest_for(launch)
        self.assertEqual(self.ack(SESSION_A, digest)[0], 200)
        self.hub.close()
        self.hub = self.start_hub()
        # The new run doesn't know the view already got its Enter, so the old digest must not match.
        self.assertEqual(self.ack(SESSION_A, digest), (409, {"error": StartupRefusal.DIGEST_MISMATCH.value}))
        cli = run_desk_cli(self.home, "ack", SESSION_A, "--digest", digest)
        self.assertEqual(cli.returncode, 1, cli.stderr)
        self.assertEqual(launch.typed(), b"\r")
        self.assertNotEqual(self.digest_for(launch), digest)


class RefusalTest(StartupCase):
    def test_not_enrolled(self):
        launch = self.launch(SESSION_C)
        self.assertNotIn(SESSION_C, self.view())
        self.assertAckRefused(launch, "0" * 64, StartupRefusal.NOT_ENROLLED)

    def test_not_blocked(self):
        launch = self.launch()
        self.tmux._run(["respawn-pane", "-k", "-t", launch.pane_id, "sh -c 'echo ready; exec sleep 600'"])
        pane = next(p for p in self.tmux.panes() if p.pane_id == launch.pane_id)
        launch = dataclasses.replace(launch, pane_pid=pane.pane_pid)
        self.write_tree(launch, self.claude_argv(SESSION_A), SESSION_A)
        wait_until(lambda: "ready" in self.tmux.capture(launch.pane_id))
        self.assertEqual(self.view(), {})
        self.assertAckRefused(launch, "0" * 64, StartupRefusal.NOT_BLOCKED)

    def test_a_digest_that_was_never_shown(self):
        self.assertAckRefused(self.launch(), hashlib.sha256(b"x").hexdigest(), StartupRefusal.DIGEST_MISMATCH)

    def test_the_screen_changed_between_view_and_ack(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        os.kill(launch.program_pid(), signal.SIGUSR2)
        wait_until(lambda: self.view()[SESSION_A]["digest"] not in (None, digest))
        self.assertAckRefused(launch, digest, StartupRefusal.DIGEST_MISMATCH)

    def test_exit_selected_is_never_entered(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        os.kill(launch.program_pid(), signal.SIGUSR1)
        wait_until(lambda: "❯ 2. Exit" in self.tmux.capture(launch.pane_id))
        self.assertAckRefused(launch, digest, StartupRefusal.EXIT_SELECTED)

    def test_no_supervisor_window_or_two_of_them(self):
        stray = self.launch(SESSION_A, tag=SESSION_C)
        self.assertAckRefused(dataclasses.replace(stray, session_id=SESSION_A), "0" * 64, StartupRefusal.NO_WINDOW)
        first = self.launch()
        digest = self.digest_for(first)
        self.launch(write_tree=False)
        self.assertEqual(self.view(), {})  # no one screen to show
        self.assertAckRefused(first, digest, StartupRefusal.AMBIGUOUS_WINDOW)

    def test_launch_arguments(self):
        cases = {
            "no mcp config": (self.claude_argv(SESSION_A)[:-4] + self.config.launch_flags[2:],
                              StartupRefusal.MISSING_FLAG),
            "no channel flag": (self.claude_argv(SESSION_A)[:-2], StartupRefusal.MISSING_FLAG),
            "other session": (self.claude_argv(SESSION_B), StartupRefusal.WRONG_SESSION),
            "id as a substring": (self.claude_argv("x" + SESSION_A), StartupRefusal.WRONG_SESSION),
            "id after =": (tuple(a if a != "--resume" else f"--resume={SESSION_A}" for a in self.claude_argv(SESSION_A)
                                 if a != SESSION_A), StartupRefusal.WRONG_SESSION),
            "two choosers": (self.claude_argv(SESSION_A) + ("--fork-session",), StartupRefusal.WRONG_SESSION),
            "not claude": (("node",) + self.claude_argv(SESSION_A)[1:], StartupRefusal.NOT_CLAUDE),
        }
        executables = {
            "another program named claude": Path(sys.executable),
            "a file beside the versions": self.config.claude_versions_dir.parent / "2.1.330",
            "an unversioned name": self.config.claude_versions_dir / "claude",
        }
        for name, exe in executables.items():
            exe.parent.mkdir(parents=True, exist_ok=True)
            if not exe.exists():
                exe.write_bytes(b"x")
            cases[name] = (self.claude_argv(SESSION_A), StartupRefusal.NOT_CLAUDE, exe)
        for name, (argv, refusal, *exe) in cases.items():
            with self.subTest(name):
                launch = self.launch(argv=argv, exe=exe[0] if exe else None)
                self.assertEqual(self.view()[SESSION_A]["refusal"], refusal.value)
                self.assertIsNone(self.view()[SESSION_A]["digest"])
                self.assertAckRefused(launch, "0" * 64, refusal)
                self.tmux.kill(launch.window_id)
                self.clock.advance(61)

    def test_pure_argv_rule(self):
        good = self.claude_argv(SESSION_A)
        self.assertIsNone(check_launch_argv(good, SESSION_A, self.config))
        self.assertEqual(check_launch_argv(good[:3] + (SESSION_A[:-1],) + good[4:], SESSION_A, self.config),
                         StartupRefusal.WRONG_SESSION)
        self.assertEqual(check_launch_argv(good + ("-c",), SESSION_A, self.config), StartupRefusal.WRONG_SESSION)
        self.assertEqual(check_launch_argv(good[:-1], SESSION_A, self.config), StartupRefusal.MISSING_FLAG)
        mcp, path, channels, server = self.config.launch_flags
        base = ("claude", "--resume", SESSION_A)
        cases = {
            "a prompt after --": (good + ("--", "--resume", SESSION_B), None),
            "flags only after --": (base + ("--", *self.config.launch_flags), StartupRefusal.MISSING_FLAG),
            "the session only after --": (("claude", *self.config.launch_flags, "--", "--resume", SESSION_A),
                                          StartupRefusal.WRONG_SESSION),
            "the MCP config twice": (good + (mcp, path), StartupRefusal.MISSING_FLAG),
            "a second MCP config with =": (good + (f"{mcp}=/tmp/other.json",), StartupRefusal.MISSING_FLAG),
            "the channel flag twice": (good + (channels, server), StartupRefusal.MISSING_FLAG),
            "another channel": (base + (mcp, path, channels, "server:other"), StartupRefusal.MISSING_FLAG),
            "--resume twice": (good + ("--resume", SESSION_A), StartupRefusal.WRONG_SESSION),
            "--resume and --session-id": (good + ("--session-id", SESSION_A), StartupRefusal.WRONG_SESSION),
            "--resume with no value": (("claude", *self.config.launch_flags, "--resume"), StartupRefusal.WRONG_SESSION),
            "the channel flag with =": (base + (mcp, path, f"{channels}={server}"), StartupRefusal.MISSING_FLAG),
            "a second channel flag with =": (good + (f"{channels}=server:other",), StartupRefusal.MISSING_FLAG),
        }
        # Beside the approved `--resume <id>`, every other spelling of a
        # session-choosing option is a second chooser.
        for spelling in ("-c", "-r", f"-r{SESSION_B}", "-rc", "-pc", f"-r={SESSION_B}", "--continue",
                         "--fork-session", "--from-pr", "--from-pr=123", f"--resume={SESSION_B}", "--resume=",
                         f"--session-id={SESSION_A}", "--session-id", f"--teleport={SESSION_B}", "--teleport",
                         "--cloud", "--cloud=x"):
            cases[f"--resume <id> and {spelling}"] = (good + (spelling,), StartupRefusal.WRONG_SESSION)
        # The approved chooser only in its exact two-argument form.
        for spelling in ((f"--resume={SESSION_A}",), ("-r", SESSION_A), (f"-r{SESSION_A}",),
                         (f"--session-id={SESSION_A}",)):
            cases[f"only {' '.join(spelling)}"] = (("claude", *spelling, *self.config.launch_flags),
                                                   StartupRefusal.WRONG_SESSION)
        cases["--session-id <id>"] = (("claude", "--session-id", SESSION_A, *self.config.launch_flags), None)
        for name, (argv, refusal) in cases.items():
            with self.subTest(name):
                self.assertEqual(check_launch_argv(argv, SESSION_A, self.config), refusal)

    def test_process_checks(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        sessions = self.config.claude_sessions_dir
        changes = {
            "session file names another session": (
                lambda: (sessions / f"{launch.claude_pid}.json").write_text(json.dumps({"sessionId": SESSION_B})),
                lambda: (sessions / f"{launch.claude_pid}.json").unlink(),
                StartupRefusal.SESSION_FILE_MISMATCH),
            "unreadable session file": (
                lambda: (sessions / f"{launch.claude_pid}.json").write_text("{"),
                lambda: (sessions / f"{launch.claude_pid}.json").unlink(),
                StartupRefusal.SESSION_FILE_MISMATCH),
            "not launched by the supervisor": (
                lambda: self.write_tree(launch, self.claude_argv(SESSION_A), SESSION_B),
                lambda: self.write_tree(launch, self.claude_argv(SESSION_A), SESSION_A),
                StartupRefusal.NOT_SUPERVISED),
            "claude exited": (
                lambda: (self.config.proc_dir / str(launch.claude_pid) / "stat").unlink(),
                lambda: self.write_tree(launch, self.claude_argv(SESSION_A), SESSION_A),
                StartupRefusal.NO_FOREGROUND),
            "two claude children": (
                lambda: self.write_process(launch.claude_pid + 50, launch.pane_pid, "9", self.claude_argv(SESSION_A),
                                           pgrp=launch.claude_pid + 50, tpgid=launch.claude_pid),
                lambda: shutil.rmtree(self.config.proc_dir / str(launch.claude_pid + 50)),
                StartupRefusal.AMBIGUOUS_PROCESS),
            "claude restarted with the same pid": (
                lambda: self.write_tree(launch, self.claude_argv(SESSION_A), SESSION_A, start="778"),
                lambda: self.write_tree(launch, self.claude_argv(SESSION_A), SESSION_A),
                StartupRefusal.DIGEST_MISMATCH),
            "session file permission denied": (
                lambda: self.session_file(launch, b"{}", mode=0),
                lambda: self.session_file(launch, None),
                StartupRefusal.SESSION_FILE_UNREADABLE),
            "session file is a FIFO (read without blocking)": (
                lambda: os.mkfifo(sessions / f"{launch.claude_pid}.json"),
                lambda: self.session_file(launch, None),
                StartupRefusal.SESSION_FILE_UNREADABLE),
            "oversize session file": (
                lambda: self.session_file(launch, json.dumps({"sessionId": SESSION_A, "pad": "x" * MAX_SESSION_FILE_BYTES})
                                          .encode()),
                lambda: self.session_file(launch, None),
                StartupRefusal.SESSION_FILE_UNREADABLE),
            "session file is a link": (
                lambda: (sessions / f"{launch.claude_pid}.json").symlink_to(sessions / "elsewhere.json"),
                lambda: self.session_file(launch, None),
                StartupRefusal.SESSION_FILE_UNREADABLE),
        }
        for name, (change, undo, refusal) in changes.items():
            with self.subTest(name):
                change()
                self.assertAckRefused(launch, digest, refusal)
                undo()
                self.clock.advance(61)
        self.assertEqual(self.digest_for(launch), digest)

    def session_file(self, launch, content, mode=0o600):
        """Write claude's session file (None removes it)."""
        path = self.config.claude_sessions_dir / f"{launch.claude_pid}.json"
        path.unlink(missing_ok=True)
        if content is not None:
            path.write_bytes(content)
            path.chmod(mode)

    def test_copy_mode(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        self.tmux._run(["copy-mode", "-t", launch.pane_id])
        self.assertAckRefused(launch, digest, StartupRefusal.PANE_IN_MODE)

    def test_the_pane_was_respawned_after_the_view(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        respawned = self.respawn(launch)
        self.assertNotEqual(respawned.pane_pid, launch.pane_pid)
        self.assertNotEqual(self.digest_for(respawned), digest)
        self.assertAckRefused(respawned, digest, StartupRefusal.DIGEST_MISMATCH)

    def test_the_screen_changes_after_the_prechecks_before_the_final_capture(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        # The prechecks' capture passes; the final one sees another screen.
        self.tmux.before_capture = [lambda: None, lambda: self.redraw_wide(launch)]
        self.assertAckRefused(launch, digest, StartupRefusal.CHANGED)

    def test_the_screen_changes_between_the_final_capture_and_the_send(self):
        launch = self.launch()
        digest = self.digest_for(launch)

        def select_exit():
            os.kill(launch.program_pid(), signal.SIGUSR1)
            wait_until(lambda: "❯ 2. Exit" in self.tmux.capture(launch.pane_id))
        # tmux's own guard sees Exit selected in the command that would press the key.
        self.tmux.before_send = [select_exit]
        self.assertAckRefused(launch, digest, StartupRefusal.CHANGED, keys_issued=1)
        self.assertEqual(self.digest_for_refusal(launch), StartupRefusal.EXIT_SELECTED.value)

    def test_the_pane_is_respawned_between_the_final_capture_and_the_send(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        # The same warning on screen, so only the guard's pane pid tells them apart.
        self.tmux.before_send = [lambda: self.respawn(launch)]
        self.assertAckRefused(launch, digest, StartupRefusal.CHANGED, keys_issued=1)

    def test_a_token_revoked_before_the_checks_finish(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        token = self.pair()
        self.tmux.before_capture = [lambda: self.hub.state.revoke()]
        self.assertEqual(self.ack(SESSION_A, digest, token=token), (401, {"error": "unauthorized"}))
        self.assertEqual(launch.typed(), b"")
        self.assertEqual(self.tmux.sent, [])

    def test_a_revoke_between_the_authorization_and_the_send_waits_for_the_enter(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        token = self.pair()
        returned = {}

        def revoke():
            response = operator_request(self.config.operator_socket, OperatorOp.REVOKE)
            returned.update(ok=response["ok"], keys_when_returned=len(self.tmux.sent), typed=launch.typed())
        revoker = threading.Thread(target=revoke, daemon=True)

        def revoke_during_final_capture():
            revoker.start()
            # Give the revoke time to delete the token and start waiting.
            wait_until(lambda: not self.hub.state.authenticate(token))
            time.sleep(0.2)
            self.assertNotIn("ok", returned)
        self.tmux.before_capture = [lambda: None, revoke_during_final_capture]
        self.assertEqual(self.ack(SESSION_A, digest, token=token), (200, {"session_id": SESSION_A, "sent": True}))
        revoker.join(timeout=15)
        # The revoke returned only once the Enter was out, never before it.
        self.assertEqual((returned["ok"], returned["keys_when_returned"]), (True, 1))
        wait_until(lambda: launch.typed() == b"\r")
        self.assertEqual(self.ack(SESSION_A, digest, token=token)[0], 401)

    def redraw_wide(self, launch):
        os.kill(launch.program_pid(), signal.SIGUSR2)
        wait_until(lambda: "─" * 130 in self.tmux.capture(launch.pane_id))

    def digest_for_refusal(self, launch):
        return self.view()[launch.session_id]["refusal"]

    def respawn(self, launch: FakeLaunch, wait=True) -> FakeLaunch:
        (launch.state / "pid").unlink()
        command = f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_PANE))} {shlex.quote(str(launch.state))}"
        self.tmux._run(["respawn-pane", "-k", "-t", launch.pane_id, command])
        pane = next(p for p in self.tmux.panes() if p.pane_id == launch.pane_id)
        again = dataclasses.replace(launch, pane_pid=pane.pane_pid, claude_pid=next(_claude_pids))
        self.write_tree(again, self.claude_argv(launch.session_id), launch.session_id)
        if wait:
            wait_until(lambda: (launch.state / "pid").exists()
                       and desk_startup_warning(self.tmux.capture(launch.pane_id)))
        return again


class GuardTest(StartupCase):
    def test_route_guards(self):
        launch = self.launch()
        body = {"session_id": SESSION_A, "digest": "0" * 64}
        port = self.port
        cases = [
            (("GET", "/desk/api/startup"), {}, 401, "unauthorized"),
            (("GET", "/desk/api/startup"), {"token": "x" * 43}, 401, "unauthorized"),
            (("GET", "/desk/api/startup"), {"token": self.token, "headers": {"Host": "127.0.0.1"}}, 403, "wrong host"),
            (("POST", "/desk/api/startup/ack", body), {}, 401, "unauthorized"),
            (("POST", "/desk/api/startup/ack", body), {"token": self.token, "headers": {"Host": f"vanpi:{port}"}},
             403, "wrong host"),
            (("POST", "/desk/api/startup/ack", body), {"token": self.token, "headers": {"Origin": "null"}},
             403, "wrong origin"),
            (("POST", "/desk/api/startup/ack", body),
             {"token": self.token, "headers": {"Origin": f"https://evil:{port}"}}, 403, "wrong origin"),
            (("POST", "/desk/api/startup/ack", body), {"token": self.token, "headers": {"Content-Type": "text/plain"}},
             400, "content type must be application/json"),
            (("POST", "/desk/api/startup/ack"), {"token": self.token, "raw": b"{"}, 400, "bad json"),
            (("POST", "/desk/api/startup/ack", {"session_id": SESSION_A, "digest": "XYZ"}), {"token": self.token},
             400, "bad digest"),
            (("POST", "/desk/api/startup/ack", {"session_id": "nope", "digest": "0" * 64}), {"token": self.token},
             400, "bad session_id"),
        ]
        for args, options, status, reason in cases:
            with self.subTest(reason=reason, args=args[:2]):
                got, headers, answer = self.call(*args, **options)
                self.assertEqual((got, answer), (status, {"error": reason}))
                self.assertFalse(any(name.startswith("access-control-") for name in headers))
        _, headers, _ = self.call("GET", "/desk/api/startup", token=self.token)
        self.assertFalse(any(name.startswith("access-control-") for name in headers))
        self.assertEqual(launch.typed(), b"")
        self.assertEqual(self.tmux.sent, [])


class ConcurrencyTest(StartupCase):
    def test_enroll_and_unenroll_alongside_acknowledgements_never_deadlock(self):
        launch = self.launch()
        digest = self.digest_for(launch)
        errors = []

        def guarded(work):
            def run():
                try:
                    work()
                except Exception as exc:  # reported below; a thread must not die silently
                    errors.append(repr(exc))
            return threading.Thread(target=run, daemon=True)

        def operator_edits():
            for _ in range(15):
                for op in (OperatorOp.ENROLL, OperatorOp.UNENROLL):
                    response = operator_request(self.config.operator_socket, op, session_id=SESSION_B)
                    assert response.get("ok") or response.get("error") == "busy", response

        def page_edits():
            for _ in range(15):
                for kind in ("enroll", "unenroll"):
                    status, _, body = self.call("POST", f"/desk/api/{kind}", {"session_id": SESSION_C}, token=self.token)
                    assert status == 200, body

        def stale_acks():
            for _ in range(MAX_STARTUP_ACKS_PER_MINUTE - 1):
                status, _ = self.ack(SESSION_A, "1" * 64)
                assert status in (409, 429), status

        def views_and_rechecks():
            for _ in range(15):
                self.call("GET", "/desk/api/startup", token=self.token)
                self.hub.recheck_now()

        threads = [guarded(work) for work in (operator_edits, page_edits, stale_acks, views_and_rechecks)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        self.assertFalse([t for t in threads if t.is_alive()], "a thread is stuck: deadlock")
        self.assertEqual(errors, [])
        self.assertEqual(launch.typed(), b"")
        self.assertEqual(self.ack(SESSION_A, digest)[0], 200)
        wait_until(lambda: launch.typed() == b"\r")
        self.assertTrue(operator_request(self.config.operator_socket, OperatorOp.STATUS)["ok"])


@unittest.skipUnless(sys.platform.startswith("linux"), "reads a real process tree from Linux /proc")
class RealProcessTreeTest(StartupCase):
    def start_hub(self, **options):
        self.config = dataclasses.replace(self.config, proc_dir=Path("/proc"))
        return super().start_hub(**options)

    def test_a_real_bash_and_claude_chain(self):
        state = Path(tempfile.mkdtemp(prefix="pane", dir=self.home))
        # The kernel's /proc/<pid>/exe must be an installed claude version, so
        # the fake runs from a copy of the interpreter placed as one (its
        # standard library found through PYTHONHOME).
        self.claude_exe.unlink()
        shutil.copy2(os.path.realpath(sys.executable), self.claude_exe)
        claude = shlex.join(("exec", "-a", "claude", str(self.claude_exe), str(FAKE_PANE), str(state),
                             "--resume", SESSION_A, *self.config.launch_flags))
        home = shlex.quote(sys.base_prefix)
        # As the supervisor's pane: bash with job control, claude a child leading the foreground group.
        command = f"exec bash --noprofile --norc -ic {shlex.quote(f'(export PYTHONHOME={home}; {claude}); exit')}"
        window_id = self.tmux.start(SESSION_A, "real", command, {MANAGED_ENV_VAR: SESSION_A})
        pane = next(p for p in self.tmux.panes() if p.window_id == window_id)
        wait_until(lambda: desk_startup_warning(self.tmux.capture(pane.pane_id)))
        item = self.view()[SESSION_A]
        self.assertIsNone(item["refusal"])
        self.assertEqual(self.ack(SESSION_A, item["digest"])[0], 200)
        wait_until(lambda: (state / "typed").exists() and (state / "typed").read_bytes() == b"\r")

    def test_another_program_named_claude_is_refused(self):
        state = Path(tempfile.mkdtemp(prefix="pane", dir=self.home))
        claude = shlex.join(("exec", "-a", "claude", sys.executable, str(FAKE_PANE), str(state),
                             "--resume", SESSION_A, *self.config.launch_flags))
        command = f"exec bash --noprofile --norc -ic {shlex.quote(f'({claude}); exit')}"
        window_id = self.tmux.start(SESSION_A, "real", command, {MANAGED_ENV_VAR: SESSION_A})
        pane = next(p for p in self.tmux.panes() if p.window_id == window_id)
        wait_until(lambda: desk_startup_warning(self.tmux.capture(pane.pane_id)))
        item = self.view()[SESSION_A]
        self.assertEqual((item["digest"], item["refusal"]), (None, StartupRefusal.NOT_CLAUDE.value))
        self.assertEqual(self.ack(SESSION_A, "0" * 64), (409, {"error": StartupRefusal.NOT_CLAUDE.value}))
        self.assertFalse((state / "typed").exists() and (state / "typed").read_bytes())
