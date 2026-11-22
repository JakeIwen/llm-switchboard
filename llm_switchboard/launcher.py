"""The shell command a kept-running session's tmux pane runs.

It goes through the user's `claudepd` bash function rather than calling claude
directly, so the session gets the same flags as an interactive `claudepd`
(bypass permissions, and the settings that skip Model Gateway, which is what
makes Remote Control available). A login + interactive bash loads that
function from ~/.bashrc and the PATH from ~/.profile, as an SSH login would.
"""

import shlex

from .settings import DEFAULTS, Settings

LAUNCH_FUNCTION = "claudepd"


def _pane(command: str, rc_name: str, launch_flags: tuple[str, ...]) -> str:
    inner = f"{command} {shlex.join(('--remote-control', rc_name, *launch_flags))}"
    return f"exec bash -lic {shlex.quote(inner)}"


def pane_command(session_id: str, cwd: str, rc_name: str, launch_flags: tuple[str, ...] = ()) -> str:
    # Manual resume settings must never change the Remote Control launch path.
    return _pane(_resume(session_id, cwd, LAUNCH_FUNCTION, "--resume"), rc_name, launch_flags)


def new_session_command(session_id: str, cwd: str, title: str, rc_name: str,
                        launch_flags: tuple[str, ...] = ()) -> str:
    """Start a conversation that doesn't exist yet, under a chosen id and title."""
    claude = shlex.join((LAUNCH_FUNCTION, "--session-id", session_id, "--name", title))
    return _pane(f"cd -- {shlex.quote(cwd)} && {claude}", rc_name, launch_flags)


def _resume(session_id: str, cwd: str, command: str, action: str) -> str:
    invocation = f"{shlex.quote(command)} {action} {shlex.quote(session_id)}"
    return f"cd -- {shlex.quote(cwd)} && {invocation}" if cwd else invocation


def resume_command(session_id: str, cwd: str, settings: Settings | None = None) -> str:
    """What to type in a shell to open the session by hand."""
    return _resume(session_id, cwd, (settings or DEFAULTS)["claude_command"], "--resume")


def codex_resume_command(thread_id: str, cwd: str, settings: Settings | None = None) -> str:
    return _resume(thread_id, cwd, (settings or DEFAULTS)["codex_command"], "resume")


_RESUME_COMMANDS = {"claude": resume_command, "codex": codex_resume_command}


def resume_for_tool(tool: str, session_id: str, cwd: str, settings: Settings | None = None) -> str:
    return _RESUME_COMMANDS[tool](session_id, cwd, settings)
