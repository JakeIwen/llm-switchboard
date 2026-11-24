"""Shared evidence for a local session identity, independent of either OS's process reader."""

from pathlib import Path
import re

from .bounded_read import read_bounded
from .shallow_json import loads_shallow

# Claude's session files are small, nearly flat objects.
MAX_SESSION_FILE_BYTES = 64 * 1024
MAX_SESSION_FILE_DEPTH = 16
_VERSION_NAME = re.compile(r"[0-9]+(?:\.[0-9]+)+")


def installed_claude_path(exe: str | None, versions_dir: Path) -> bool:
    """The kernel's executable path names a version directly inside the resolved install root."""
    if exe is None:
        return False
    path = Path(exe)
    return path.parent == versions_dir and _VERSION_NAME.fullmatch(path.name) is not None


def read_session_file(sessions_dir: Path, pid: int, *, min_mtime_ns: int | None = None) -> dict | None:
    """One bounded read; no entry on absence, staleness, non-regular file or invalid JSON."""
    try:
        data = read_bounded(Path(sessions_dir) / f"{pid}.json", MAX_SESSION_FILE_BYTES, min_mtime_ns=min_mtime_ns)
        entry = loads_shallow(data, MAX_SESSION_FILE_DEPTH) if data is not None else None
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None
