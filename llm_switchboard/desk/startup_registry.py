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

from pathlib import Path

from .bounded_read import read_bounded
from .peer import MAX_SESSION_FILE_BYTES, MAX_SESSION_FILE_DEPTH
from .shallow_json import loads_shallow
from .startup_identity import StartupRefusal


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
        entry = loads_shallow(data, MAX_SESSION_FILE_DEPTH)
    except ValueError:
        # Claude writes this file: nesting too deep to parse safely is no
        # more a session entry than bad JSON.
        return True, None
    return True, entry if isinstance(entry, dict) else None
