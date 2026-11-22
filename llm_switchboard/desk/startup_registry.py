"""Reading claude's `sessions/<pid>.json` for the startup acknowledgement
(docs/voice-desk.md, "Startup acknowledgement").

At the warning the file normally doesn't exist yet, so absence must be
told apart from a file that can't be read: only a definitive ENOENT is
absence, and every other failure refuses. One open, never following a
link and never blocking (a FIFO in its place would otherwise stall the
hub), then fstat for a regular file and a bounded read.
Standard library only.
"""

import json
import os
from pathlib import Path
import stat

from .startup_identity import StartupRefusal

# Claude's entries are a few hundred bytes.
MAX_SESSION_FILE_BYTES = 64 * 1024
_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def read_session_entry(sessions_dir: Path, pid: int) -> tuple[bool, dict | None] | StartupRefusal:
    """`(present, entry)` for `check_session_file`: `(False, None)` when the
    file doesn't exist, `(True, None)` when it holds no JSON object;
    SESSION_FILE_UNREADABLE when it can't be inspected."""
    try:
        fd = os.open(Path(sessions_dir) / f"{pid}.json", _OPEN_FLAGS)
    except FileNotFoundError:
        return False, None
    except OSError:
        return StartupRefusal.SESSION_FILE_UNREADABLE
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SESSION_FILE_BYTES:
            return StartupRefusal.SESSION_FILE_UNREADABLE
        data = _read_at_most(fd, MAX_SESSION_FILE_BYTES + 1)
    except OSError:
        return StartupRefusal.SESSION_FILE_UNREADABLE
    finally:
        os.close(fd)
    if len(data) > MAX_SESSION_FILE_BYTES:
        return StartupRefusal.SESSION_FILE_UNREADABLE
    try:
        entry = json.loads(data)
    except (ValueError, RecursionError):
        # Claude writes this file: deep nesting under the size cap still
        # overflows the parser, and is no more a session entry than bad JSON.
        return True, None
    return True, entry if isinstance(entry, dict) else None


def _read_at_most(fd: int, limit: int) -> bytes:
    chunks, size = [], 0
    while size < limit:
        chunk = os.read(fd, limit - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)
