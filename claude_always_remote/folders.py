"""Folders a new session may be started in.

Only folders that already have conversations, still exist, and that Claude
Code already trusts: a new session started anywhere else would stop at the
folder trust prompt with nobody at the terminal. The home folder is never
offered; Claude Code asks about it every time.
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
    # Why it can't be used, or "" if it can.
    blocked: str


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
                        home: Path = HOME) -> list[Folder]:
    counts: dict[str, int] = {}
    for t in transcripts:
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
        folders.append(Folder(cwd=cwd, project=display_path(cwd), sessions=n, blocked=blocked))
    return sorted(folders, key=lambda f: (bool(f.blocked), -f.sessions, f.cwd))
