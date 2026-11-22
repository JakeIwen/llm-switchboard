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


# Ink reflows paragraph text, but the header, two selectable options and
# footer are separate rows. A quoted paragraph in a normal prompt isn't a dialog.
_WARNING_TEXT = ("--dangerously-load-development-channels is for local channel development only. "
                 "Do not use this option to run channels you have downloaded off the internet.")
_WARNING_OPTIONS = {
    "❯ 1. I am using this for local development\n2. Exit",
    "1. I am using this for local development\n❯ 2. Exit",
}


def desk_startup_warning(screen: str) -> bool:
    """A display-only observation: matching pixels do not authorize sending a key."""
    title = "WARNING: Loading development channels"
    if len(screen) > 16_384 or title not in screen:
        return False
    rows = "\n".join(line.strip(" ") for line in screen.strip(" \n").split("\n"))
    blocks = re.split(r"\n{2,}", rows)
    if len(blocks) != 6:
        return False
    header, description, advice, channels, options, footer = blocks
    return (re.fullmatch("─{12,}\n" + title, header) is not None
            and description.replace("\n", " ") == _WARNING_TEXT
            and advice.replace("\n", " ") == "Please use --channels to run a list of approved channels."
            and channels == "Channels: server:desk"
            and options in _WARNING_OPTIONS
            and footer == "Enter to confirm · Esc to cancel")


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
