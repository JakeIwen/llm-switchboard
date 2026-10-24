"""Locations and names the other modules agree on."""

from pathlib import Path
import os

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
# Claude Code's own settings and per-folder state, including folder trust.
CLAUDE_STATE_FILE = HOME / ".claude.json"
# One <session-id>.jsonl transcript per conversation, grouped by project folder.
PROJECTS_DIR = CLAUDE_DIR / "projects"
# Claude Code writes <pid>.json here for every running process.
LIVE_REGISTRY_DIR = CLAUDE_DIR / "sessions"

CODEX_DIR = HOME / ".codex"
# Codex's own settings, including which folders it trusts.
CODEX_CONFIG_FILE = CODEX_DIR / "config.toml"


def _codex_socket() -> Path | None:
    """The Codex daemon's control socket. LLM_SWITCHBOARD_CODEX_SOCKET
    points elsewhere (tests use a fake daemon); set it empty to leave Codex out."""
    configured = os.environ.get("LLM_SWITCHBOARD_CODEX_SOCKET")
    if configured is not None:
        return Path(configured) if configured else None
    return CODEX_DIR / "app-server-control" / "app-server-control.sock"


CODEX_SOCKET = _codex_socket()

STATE_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "llm-switchboard"
STORE_FILE = STATE_DIR / "sessions.json"
SETTINGS_FILE = STATE_DIR / "settings.json"
# Codex sessions deleted here are archived in Codex; this remembers which ones,
# so they show under "Recently deleted" and are deleted for good after 30 days.
CODEX_TRASH_FILE = STATE_DIR / "codex-trash.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share") / "llm-switchboard"
TRASH_DIR = DATA_DIR / "trash"
# Shared by the page and CLI; WAL lets searches continue while the child indexer writes.
SEARCH_INDEX_FILE = DATA_DIR / "transcript-search.sqlite3"

HOSTS_DIR = TRASH_DIR.parent / "hosts"

# A dedicated tmux server keeps these windows apart from any tmux the user runs.
# A second instance (development, browser tests) must use its own socket, or its
# supervisor would close the real instance's windows as "not enabled".
TMUX_SOCKET = os.environ.get("LLM_SWITCHBOARD_TMUX_SOCKET") or "llm-switchboard"
TMUX_SESSION = "switchboard"
TMUX_CONF = Path(__file__).resolve().parent / "tmux.conf"

# This install's machine names, kept out of git (local_config.py, local.env.example).
LOCAL_ENV = Path(__file__).resolve().parents[1] / "local.env"

# Set in every pane we launch, so a claude process can be traced back to us
# through /proc/<pid>/environ.
MANAGED_ENV_VAR = "LLM_SWITCHBOARD_SESSION"

# Never the LAN: "tailscale" is this machine's Tailscale address (listen.py).
DEFAULT_HOSTS = ("127.0.0.1", "tailscale")
DEFAULT_PORT = 8777
