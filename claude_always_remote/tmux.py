"""The tmux server that hosts the kept-running sessions, one window each.

Windows are tagged with the user option @car_session so they can be matched to
session ids regardless of their visible names.
"""

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess
import time

from .paths import TMUX_CONF, TMUX_SESSION, TMUX_SOCKET

_FIELDS = ["window_id", "@car_session", "window_name", "pane_pid", "pane_dead", "pane_dead_status"]


@dataclass(frozen=True)
class Window:
    window_id: str
    session_id: str
    label: str
    pane_pid: int | None
    dead: bool
    exit_status: int | None


class TmuxError(RuntimeError):
    pass


class Tmux:
    def __init__(self, socket: str = TMUX_SOCKET, session: str = TMUX_SESSION, conf: Path = TMUX_CONF):
        self.socket = socket
        self.session = session
        self.conf = Path(conf)

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
            window_id, session_id, label, pane_pid, dead, status = parts
            found.append(
                Window(
                    window_id=window_id,
                    session_id=session_id,
                    label=label,
                    pane_pid=int(pane_pid) if pane_pid.isdigit() else None,
                    dead=dead == "1",
                    exit_status=int(status) if status.lstrip("-").isdigit() else None,
                )
            )
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

    def capture(self, window_id: str, history: int = 0) -> str:
        """The pane's visible text, plus up to `history` lines of scrollback."""
        args = ["capture-pane", "-p", "-J", "-t", window_id]
        if history:
            args += ["-S", f"-{history}"]
        result = self._run(args, check=False)
        return result.stdout if result.returncode == 0 else ""

    def send_line(self, window_id: str, text: str) -> None:
        """Type text into the pane literally, then press Enter."""
        self._run(["send-keys", "-t", window_id, "-l", text])
        self._run(["send-keys", "-t", window_id, "Enter"])

    def kill_server(self) -> None:
        self._run(["kill-server"], check=False)

    def attach_argv(self, window_id: str) -> list[str]:
        return [*self.base_argv(), "attach", "-t", f"={self.session}", ";", "select-window", "-t", window_id]

    def _run(self, args: list[str], check: bool = True, new_server: bool = False) -> subprocess.CompletedProcess:
        argv = self.base_argv() + args
        if new_server:
            argv = _own_scope() + argv
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
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
    unit = f"claude-always-remote-tmux-{int(time.time())}"
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
