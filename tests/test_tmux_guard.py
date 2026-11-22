"""`Tmux.send_enter_if` and the strict pane listing, against a real isolated
tmux server: the guard is evaluated by tmux itself, so only a real server
shows that its format and escaping mean what the desk intends."""

import itertools
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import unittest

from desk_fixtures import wait_until

from llm_switchboard.tmux import GuardedSend, PaneGuard, Tmux, TmuxError

_sockets = itertools.count(1)
# Every character tmux formats or fnmatch treat specially, on one row.
ODD_ROW = "a,b}c#{d}*e?f[g]h\\i ❯ 1. x"


class GuardedSendTest(unittest.TestCase):
    def setUp(self):
        self.tmux = Tmux(socket=f"tmux-guard-{os.getpid()}-{next(_sockets)}", conf=Path(os.devnull), timeout=5)
        self.socket_file = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / self.tmux.socket
        self.addCleanup(self.socket_file.unlink, missing_ok=True)
        self.addCleanup(self.tmux.kill_server)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.typed = self.dir / "typed"
        # Cooked mode: the Enter completes a line, which `cat` then writes.
        script = f"printf '%s\\n' {shlex.quote(ODD_ROW)}; exec cat > {shlex.quote(str(self.typed))}"
        self.tmux.start("guard-test", "guard", f"sh -c {shlex.quote(script)}", {})
        self.pane = next(p for p in self.tmux.panes() if p.session_id == "guard-test")
        wait_until(lambda: ODD_ROW in self.tmux.capture(self.pane.pane_id))

    def guard(self, **changes):
        options = {"expect": (("pane_id", self.pane.pane_id), ("pane_pid", str(self.pane.pane_pid)),
                              ("@car_session", "guard-test"), ("pane_in_mode", "0")),
                   "present": (ODD_ROW,), "absent": ("not on screen",)}
        return PaneGuard(**{**options, **changes})

    def test_sends_when_every_condition_holds(self):
        self.assertIs(self.tmux.send_enter_if(self.pane.pane_id, self.guard()), GuardedSend.SENT)
        wait_until(lambda: self.typed.exists() and self.typed.read_bytes() == b"\n")

    def test_types_nothing_when_any_condition_fails(self):
        cases = {
            "another pane pid": self.guard(expect=(("pane_pid", str(self.pane.pane_pid + 1)),)),
            "another tag": self.guard(expect=(("@car_session", "guard-test,x"),)),
            "a row that isn't shown": self.guard(present=(ODD_ROW + "!",)),
            "a row that must be absent": self.guard(absent=(ODD_ROW,)),
            "a wildcard is literal": self.guard(present=("a,b}c#{d}*?f",)),
        }
        for name, guard in cases.items():
            with self.subTest(name):
                self.assertIs(self.tmux.send_enter_if(self.pane.pane_id, guard), GuardedSend.REFUSED)
        missing = self.guard(expect=(("pane_id", "%999"),))
        self.assertIs(self.tmux.send_enter_if("%999", missing), GuardedSend.REFUSED)
        self.tmux._run(["copy-mode", "-t", self.pane.pane_id])
        self.assertIs(self.tmux.send_enter_if(self.pane.pane_id, self.guard()), GuardedSend.REFUSED)
        self.assertFalse(self.typed.exists() and self.typed.read_bytes())

    def test_no_server_lists_no_panes_but_a_socket_it_may_not_open_raises(self):
        idle = Tmux(socket=f"tmux-guard-none-{os.getpid()}", conf=Path(os.devnull), timeout=5)
        self.assertEqual(idle.panes(), [])
        self.socket_file.chmod(0)
        self.addCleanup(self.socket_file.chmod, 0o600)
        with self.assertRaises(TmuxError):
            self.tmux.panes()
        with self.assertRaises(TmuxError):
            self.tmux.capture(self.pane.pane_id, check=True)
        self.assertEqual(self.tmux.capture(self.pane.pane_id), "")


if __name__ == "__main__":
    unittest.main()
