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


def pane_command(session_id: str, cwd: str, rc_name: str) -> str:
    # Manual resume settings must never change the Remote Control launch path.
    command = _resume(session_id, cwd, LAUNCH_FUNCTION, "--resume")
    inner = f"{command} --remote-control {shlex.quote(rc_name)}"
    return f"exec bash -lic {shlex.quote(inner)}"


def new_session_command(session_id: str, cwd: str, title: str, rc_name: str) -> str:
    """Start a conversation that doesn't exist yet, under a chosen id and title."""
    claude = (f"{LAUNCH_FUNCTION} --session-id {shlex.quote(session_id)} --name {shlex.quote(title)}"
              f" --remote-control {shlex.quote(rc_name)}")
    return f"exec bash -lic {shlex.quote(f'cd -- {shlex.quote(cwd)} && {claude}')}"


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
