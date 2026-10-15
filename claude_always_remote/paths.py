"""Locations and names the other modules agree on."""

from pathlib import Path
import os

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
# One <session-id>.jsonl transcript per conversation, grouped by project folder.
PROJECTS_DIR = CLAUDE_DIR / "projects"
# Claude Code writes <pid>.json here for every running process.
LIVE_REGISTRY_DIR = CLAUDE_DIR / "sessions"

STATE_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "claude-always-remote"
STORE_FILE = STATE_DIR / "sessions.json"
TRASH_DIR = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share") / "claude-always-remote" / "trash"

# A dedicated tmux server keeps these windows apart from any tmux the user runs.
# A second instance (development, browser tests) must use its own socket, or its
# supervisor would close the real instance's windows as "not enabled".
TMUX_SOCKET = os.environ.get("CLAUDE_ALWAYS_REMOTE_TMUX_SOCKET") or "claude-always-remote"
TMUX_SESSION = "car"
TMUX_CONF = Path(__file__).resolve().parent / "tmux.conf"

# Set in every pane we launch, so a claude process can be traced back to us
# through /proc/<pid>/environ.
MANAGED_ENV_VAR = "CLAUDE_ALWAYS_REMOTE_SESSION"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8777
