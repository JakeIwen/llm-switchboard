"""The shell command a kept-running session's tmux pane runs.

It goes through the user's `claudepd` bash function rather than calling claude
directly, so the session gets the same flags as an interactive `claudepd`
(bypass permissions, and the settings that skip Model Gateway, which is what
makes Remote Control available). A login + interactive bash loads that
function from ~/.bashrc and the PATH from ~/.profile, as an SSH login would.
"""

import shlex

LAUNCH_FUNCTION = "claudepd"


def pane_command(session_id: str, cwd: str, rc_name: str) -> str:
    claude = f"{LAUNCH_FUNCTION} --resume {shlex.quote(session_id)} --remote-control {shlex.quote(rc_name)}"
    inner = f"cd -- {shlex.quote(cwd)} && {claude}" if cwd else claude
    return f"exec bash -lic {shlex.quote(inner)}"
