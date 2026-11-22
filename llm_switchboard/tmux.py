"""The tmux server that hosts the kept-running sessions, one window each.

Windows are tagged with the user option @car_session so they can be matched to
session ids regardless of their visible names.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import os
import shutil
import subprocess
import time

from .paths import TMUX_CONF, TMUX_SESSION, TMUX_SOCKET

_FIELDS = ["window_id", "@car_session", "window_name", "pane_pid", "pane_dead", "pane_dead_status",
           "window_active_clients"]
_PANE_FIELDS = ["window_id", "@car_session", "pane_id", "pane_pid", "pane_dead", "pane_in_mode"]


@dataclass(frozen=True)
class Window:
    window_id: str
    session_id: str
    label: str
    pane_pid: int | None
    dead: bool
    exit_status: int | None
    # Terminals attached and showing this window right now.
    viewers: int = 0


@dataclass(frozen=True)
class Pane:
    """One pane, keyed by the window's @car_session tag that `start` sets."""
    window_id: str
    session_id: str
    pane_id: str
    pane_pid: int | None
    dead: bool
    # Copy or view mode: keys go to tmux, not to the program.
    in_mode: bool


class TmuxError(RuntimeError):
    pass


class GuardedSend(str, Enum):
    """What `send_enter_if` reports: the server pressed the key, or its
    guard was false and nothing was typed."""
    SENT = "desk-enter-sent"
    REFUSED = "desk-enter-refused"


@dataclass(frozen=True)
class PaneGuard:
    """A condition the tmux server evaluates on a pane in the same command
    as the key, so no pane output is processed between the test and the
    send. `expect` maps format variables (`pane_pid`, `@car_session`, ...)
    to their exact values; `present` and `absent` are visible rows the
    screen must and must not contain (tmux's `#{C:}` pane search)."""
    expect: tuple[tuple[str, str], ...]
    present: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()

    def format(self) -> str:
        terms = [f"#{{==:#{{{name}}},{_format_literal(value)}}}" for name, value in self.expect]
        terms += [f"#{{C:{_search_literal(row)}}}" for row in self.present]
        terms += [f"#{{==:#{{C:{_search_literal(row)}}},0}}" for row in self.absent]
        if not terms:
            raise ValueError("an empty guard")
        condition = terms[-1]
        for term in reversed(terms[:-1]):
            condition = f"#{{&&:{term},{condition}}}"
        return condition


def _format_literal(text: str) -> str:
    """Text taken literally inside a tmux format argument."""
    return text.replace("#", "##").replace(",", "#,").replace("}", "#}")


def _search_literal(text: str) -> str:
    """A `#{C:}` term: tmux wraps it as the fnmatch pattern `*term*` over each visible row."""
    escaped = "".join("\\" + c if c in "*?[\\" else c for c in text)
    return _format_literal(escaped)


def _no_server(stderr: str) -> bool:
    """tmux's answers when nothing serves the socket: the normal empty state,
    unlike a socket it may not open (permission denied) or another failure."""
    return ("no server running" in stderr
            or ("error connecting to" in stderr and ("No such file or directory" in stderr
                                                     or "Connection refused" in stderr)))


class Tmux:
    def __init__(self, socket: str = TMUX_SOCKET, session: str = TMUX_SESSION, conf: Path = TMUX_CONF,
                 timeout: float = 20):
        self.socket = socket
        self.session = session
        self.conf = Path(conf)
        # Per tmux call; the desk hub uses a short one while it holds a file lock.
        self.timeout = timeout

    def base_argv(self) -> list[str]:
        return ["tmux", "-L", self.socket, "-f", str(self.conf)]

    def windows(self) -> list[Window]:
        fmt = "\t".join("#{%s}" % f for f in _FIELDS)
        result = self._run(["list-windows", "-a", "-F", fmt], check=False)
        if result.returncode != 0:
            # No server (nothing running yet) is the normal empty state.
            return []
        found = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != len(_FIELDS) or not parts[1]:
                continue
            window_id, session_id, label, pane_pid, dead, status, viewers = parts
            found.append(
                Window(
                    window_id=window_id,
                    session_id=session_id,
                    label=label,
                    pane_pid=int(pane_pid) if pane_pid.isdigit() else None,
                    dead=dead == "1",
                    exit_status=int(status) if status.lstrip("-").isdigit() else None,
                    viewers=int(viewers) if viewers.isdigit() else 0,
                )
            )
        return found

    def panes(self) -> list[Pane]:
        """Every pane of a tagged window; none when there is no server. Any
        other failure (a socket it may not open, a tmux error) raises
        TmuxError: the desk must not read it as "nothing is waiting"."""
        fmt = "\t".join("#{%s}" % f for f in _PANE_FIELDS)
        result = self._run(["list-panes", "-a", "-F", fmt], check=False)
        if result.returncode != 0:
            if _no_server(result.stderr):
                return []
            raise TmuxError(f"list-panes failed: {result.stderr.strip()}")
        found = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != len(_PANE_FIELDS) or not parts[1]:
                continue
            window_id, session_id, pane_id, pane_pid, dead, in_mode = parts
            found.append(Pane(window_id=window_id, session_id=session_id, pane_id=pane_id,
                              pane_pid=int(pane_pid) if pane_pid.isdigit() else None,
                              dead=dead == "1", in_mode=in_mode != "0"))
        return found

    def start(self, session_id: str, label: str, command: str, env: dict[str, str]) -> str:
        """Open a window running command and return its window id."""
        env_args = [arg for k, v in env.items() for arg in ("-e", f"{k}={v}")]
        common = ["-d", "-P", "-F", "#{window_id}", "-n", label, *env_args, "-c", str(Path.home())]
        if self._run(["has-session", "-t", f"={self.session}"], check=False).returncode == 0:
            result = self._run(["new-window", "-t", f"={self.session}:", *common, command])
        else:
            result = self._run(["new-session", "-s", self.session, *common, command], new_server=True)
        window_id = result.stdout.strip()
        self._run(["set-option", "-w", "-t", window_id, "@car_session", session_id])
        return window_id

    def kill(self, window_id: str) -> None:
        self._run(["kill-window", "-t", window_id], check=False)

    def capture(self, window_id: str, history: int = 0, check: bool = False) -> str:
        """The pane's visible text, plus up to `history` lines of scrollback.
        A failure reads as an empty screen, or raises TmuxError with `check`."""
        args = ["capture-pane", "-p", "-J", "-t", window_id]
        if history:
            args += ["-S", f"-{history}"]
        result = self._run(args, check=check)
        return result.stdout if result.returncode == 0 else ""

    def send_line(self, window_id: str, text: str) -> None:
        """Type text into the pane literally, then press Enter."""
        self._run(["send-keys", "-t", window_id, "-l", text])
        self._run(["send-keys", "-t", window_id, "Enter"])

    def send_enter(self, pane_id: str) -> None:
        """Press Enter once in one pane (a `%<n>` pane id), typing nothing else."""
        self._run(["send-keys", "-t", pane_id, "Enter"])

    def send_enter_if(self, pane_id: str, guard: PaneGuard) -> GuardedSend:
        """Press Enter once in one pane only if `guard` holds there, decided
        by the server in one command: `if-shell -F` evaluates the format
        without a shell and runs its branch in the same command queue pass,
        so the pane's screen and process can't change between the test and
        the key. Raises TmuxError (or a timeout) when the outcome is unknown;
        a key may then have been delivered."""
        marker = "display-message -p {}"
        sent = f"send-keys -t {pane_id} Enter ; {marker.format(GuardedSend.SENT.value)}"
        result = self._run(["if-shell", "-t", pane_id, "-F", guard.format(), sent,
                            marker.format(GuardedSend.REFUSED.value)], check=False)
        answer = result.stdout.strip()
        if result.returncode == 0 and answer in (GuardedSend.SENT.value, GuardedSend.REFUSED.value):
            return GuardedSend(answer)
        raise TmuxError(f"if-shell: exit {result.returncode}: {result.stderr.strip()[:120]}")

    def kill_server(self) -> None:
        self._run(["kill-server"], check=False)

    def attach_argv(self, window_id: str) -> list[str]:
        return [*self.base_argv(), "attach", "-t", f"={self.session}", ";", "select-window", "-t", window_id]

    def _run(self, args: list[str], check: bool = True, new_server: bool = False) -> subprocess.CompletedProcess:
        argv = self.base_argv() + args
        if new_server:
            argv = _own_scope() + argv
        result = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        if check and result.returncode != 0:
            raise TmuxError(f"{' '.join(args[:1])} failed: {result.stderr.strip()}")
        return result


def _own_scope() -> list[str]:
    """Prefix that starts the tmux server in its own systemd scope.

    Under the systemd service, a server forked directly would live in the
    service's cgroup and be killed on every service restart, taking all the
    Claude sessions with it.
    """
    if not os.environ.get("INVOCATION_ID") or not shutil.which("systemd-run"):
        return []
    unit = f"llm-switchboard-tmux-{int(time.time())}"
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
