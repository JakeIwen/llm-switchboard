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
# The options block, by which option the cursor is on.
_WARNING_OPTIONS = {
    "❯ 1. I am using this for local development\n2. Exit": 1,
    "1. I am using this for local development\n❯ 2. Exit": 2,
}
# Enter on this option accepts the warning; on the other it exits Claude.
DESK_WARNING_ACCEPT_OPTION = 1
_DESK_WARNING_TITLE = "WARNING: Loading development channels"
_DESK_WARNING_CHANNELS = "Channels: server:desk"
_DESK_WARNING_FOOTER = "Enter to confirm · Esc to cancel"
_ACCEPT_SELECTED = next(key for key, option in _WARNING_OPTIONS.items()
                        if option == DESK_WARNING_ACCEPT_OPTION).split("\n")[0]
# Rows tmux checks on its own screen in the same command as the desk's Enter
# (`tmux.PaneGuard`): the warning with the accept option selected must show,
# and the Exit option must not be the selected one. Ink doesn't reflow these
# rows, so each is one visible line.
DESK_WARNING_GUARD_PRESENT = (_DESK_WARNING_TITLE, _DESK_WARNING_CHANNELS, _ACCEPT_SELECTED, _DESK_WARNING_FOOTER)
DESK_WARNING_GUARD_ABSENT = (next(key for key, option in _WARNING_OPTIONS.items()
                                  if option != DESK_WARNING_ACCEPT_OPTION).split("\n")[1],)


def desk_startup_selection(screen: str) -> int | None:
    """The selected option when `screen` is exactly the development-channel
    warning, else None. A display-only observation: matching text does not
    by itself authorize sending a key (the desk's acknowledgement adds its
    launch-identity checks)."""
    title = _DESK_WARNING_TITLE
    if len(screen) > 16_384 or title not in screen:
        return None
    rows = "\n".join(line.strip(" ") for line in screen.strip(" \n").split("\n"))
    blocks = re.split(r"\n{2,}", rows)
    if len(blocks) != 6:
        return None
    header, description, advice, channels, options, footer = blocks
    matches = (re.fullmatch("─{12,}\n" + title, header) is not None
               and description.replace("\n", " ") == _WARNING_TEXT
               and advice.replace("\n", " ") == "Please use --channels to run a list of approved channels."
               and channels == _DESK_WARNING_CHANNELS
               and footer == _DESK_WARNING_FOOTER)
    return _WARNING_OPTIONS.get(options) if matches else None


def desk_startup_warning(screen: str) -> bool:
    return desk_startup_selection(screen) is not None


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
