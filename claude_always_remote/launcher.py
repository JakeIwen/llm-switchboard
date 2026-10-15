"""The shell command a kept-running session's tmux pane runs.

It goes through the user's `claudepd` bash function rather than calling claude
directly, so the session gets the same flags as an interactive `claudepd`
(bypass permissions, and the settings that skip Model Gateway, which is what
makes Remote Control available). A login + interactive bash loads that
function from ~/.bashrc and the PATH from ~/.profile, as an SSH login would.
"""

import shlex

LAUNCH_FUNCTION = "claudepd"
# Codex sessions aren't launched here, only offered as a command to copy.
CODEX_FUNCTION = "codexp"


def pane_command(session_id: str, cwd: str, rc_name: str) -> str:
    inner = f"{resume_command(session_id, cwd)} --remote-control {shlex.quote(rc_name)}"
    return f"exec bash -lic {shlex.quote(inner)}"


def new_session_command(session_id: str, cwd: str, title: str, rc_name: str) -> str:
    """Start a conversation that doesn't exist yet, under a chosen id and title."""
    claude = (f"{LAUNCH_FUNCTION} --session-id {shlex.quote(session_id)} --name {shlex.quote(title)}"
              f" --remote-control {shlex.quote(rc_name)}")
    return f"exec bash -lic {shlex.quote(f'cd -- {shlex.quote(cwd)} && {claude}')}"


def resume_command(session_id: str, cwd: str) -> str:
    """What to type in a shell to open the session by hand."""
    claude = f"{LAUNCH_FUNCTION} --resume {shlex.quote(session_id)}"
    return f"cd -- {shlex.quote(cwd)} && {claude}" if cwd else claude


def codex_resume_command(thread_id: str, cwd: str) -> str:
    codex = f"{CODEX_FUNCTION} resume {shlex.quote(thread_id)}"
    return f"cd -- {shlex.quote(cwd)} && {codex}" if cwd else codex
