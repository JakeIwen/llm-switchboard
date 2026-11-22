"""Reading claude's `sessions/<pid>.json` for the startup acknowledgement
(docs/voice-desk.md, "Startup acknowledgement").

At the warning the file normally doesn't exist yet, so absence must be
told apart from a file that can't be read: only a definitive ENOENT is
absence, and every other failure refuses. The read itself is
`bounded_read.read_bounded`: one open, never following a link and never
blocking (a FIFO in its place would otherwise stall the hub), a regular
file only, and bounded.
Standard library only.
"""

import json
from pathlib import Path

from .bounded_read import read_bounded
from .startup_identity import StartupRefusal

# Claude's entries are a few hundred bytes.
MAX_SESSION_FILE_BYTES = 64 * 1024


def read_session_entry(sessions_dir: Path, pid: int) -> tuple[bool, dict | None] | StartupRefusal:
    """`(present, entry)` for `check_session_file`: `(False, None)` when the
    file doesn't exist, `(True, None)` when it holds no JSON object;
    SESSION_FILE_UNREADABLE when it can't be inspected."""
    try:
        data = read_bounded(Path(sessions_dir) / f"{pid}.json", MAX_SESSION_FILE_BYTES)
    except OSError:
        return StartupRefusal.SESSION_FILE_UNREADABLE
    if data is None:
        return False, None
    try:
        entry = json.loads(data)
    except (ValueError, RecursionError):
        # Claude writes this file: deep nesting under the size cap still
        # overflows the parser, and is no more a session entry than bad JSON.
        return True, None
    return True, entry if isinstance(entry, dict) else None
