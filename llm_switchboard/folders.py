"""Folders a new session may be started in.

Only folders that already have conversations (Claude Code's or Codex's) and
still exist. A Claude Code session also needs a folder Claude Code already
trusts: anywhere else it would stop at the folder trust prompt with nobody at
the terminal, and in the home folder Claude Code asks every time. A Codex
session runs in the Codex daemon, which has no such prompt.
"""

from dataclasses import dataclass
from pathlib import Path
import json
import os

from .paths import CLAUDE_STATE_FILE, HOME
from .transcripts import Transcript, display_path


@dataclass(frozen=True)
class Folder:
    cwd: str
    project: str
    sessions: int
    # Why a Claude Code session can't start here, or "" if it can.
    blocked: str
    # The same for a Codex session.
    codex_blocked: str = ""


def trusted_paths(state_file: Path = CLAUDE_STATE_FILE) -> set[str]:
    """Folders whose trust prompt has been accepted, from Claude Code's ~/.claude.json."""
    try:
        projects = json.loads(Path(state_file).read_text()).get("projects", {})
    except (OSError, ValueError):
        # Claude Code rewrites this file often; a torn read counts as "nothing trusted".
        return set()
    return {path for path, cfg in projects.items() if isinstance(cfg, dict) and cfg.get("hasTrustDialogAccepted")}


def is_trusted(cwd: str, trusted: set[str]) -> bool:
    """Trust carries down to subfolders, as Claude Code applies it."""
    path = Path(cwd)
    return any(str(p) in trusted for p in (path, *path.parents))


def new_session_folders(transcripts: list[Transcript], state_file: Path = CLAUDE_STATE_FILE,
                        home: Path = HOME, *, codex=()) -> list[Folder]:
    """codex: Codex threads (anything with a .cwd), counted alongside the transcripts."""
    counts: dict[str, int] = {}
    for t in [*transcripts, *codex]:
        if t.cwd:
            counts[t.cwd] = counts.get(t.cwd, 0) + 1
    trusted = trusted_paths(state_file)
    folders = []
    for cwd, n in counts.items():
        if os.path.normpath(cwd) == os.path.normpath(home):
            blocked = "Claude Code asks for folder trust in ~ every time."
        elif not os.path.isdir(cwd):
            blocked = "The folder no longer exists."
        elif not is_trusted(cwd, trusted):
            blocked = "Not trusted yet; open Claude Code there once in a terminal to accept the trust prompt."
        else:
            blocked = ""
        codex_blocked = "" if os.path.isdir(cwd) else "The folder no longer exists."
        folders.append(Folder(cwd=cwd, project=display_path(cwd), sessions=n, blocked=blocked, codex_blocked=codex_blocked))
    return sorted(folders, key=lambda f: (bool(f.blocked) and bool(f.codex_blocked), -f.sessions, f.cwd))


def existing_folder(cwd) -> str:
    """A folder typed by the user, as an absolute path that exists."""
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("folder must be a non-empty path")
    path = os.path.normpath(os.path.expanduser(cwd.strip()))
    if not os.path.isabs(path):
        raise ValueError("folder must be an absolute path (or start with ~)")
    if not os.path.isdir(path):
        raise ValueError(f"{path} is not an existing folder")
    return path
