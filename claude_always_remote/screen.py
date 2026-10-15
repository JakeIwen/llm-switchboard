"""Reading a Claude Code terminal screen captured from tmux.

Claude Code puts a Remote Control indicator at the right end of its status
line: "/rc active", "/rc connecting…", "/rc reconnecting…" or "/rc failed".
A bare "/rc" also means connected (shown while a turn runs, and by new
sessions).
"""

from enum import Enum
import re

# Only the bottom of the screen holds the status line; scrollback above it can
# contain "/rc" typed as a command.
_STATUS_LINES = 8
_RC_RE = re.compile(r"(?:^|\s)/rc(?: (active|connecting|reconnecting|failed))?(?:…|\.\.\.)?\s*$")

# Prompts that block startup until someone answers them in the terminal.
_BLOCKING_PROMPTS = {
    "trust this folder": "Folder trust prompt is waiting; attach to answer it.",
    "Enter to confirm": "A prompt is waiting for input; attach to answer it.",
}


class Rc(str, Enum):
    ACTIVE = "active"
    CONNECTING = "connecting"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


def rc_indicator(screen: str) -> Rc | None:
    """The Remote Control state from the status line, or None if it isn't shown."""
    lines = [line for line in screen.splitlines() if line.strip()]
    for line in reversed(lines[-_STATUS_LINES:]):
        match = _RC_RE.search(line)
        if match:
            return Rc(match.group(1) or Rc.ACTIVE.value)
    return None


def blocking_prompt(screen: str) -> str | None:
    """A message describing a prompt waiting for input, if one is on screen."""
    return next((msg for text, msg in _BLOCKING_PROMPTS.items() if text in screen), None)
