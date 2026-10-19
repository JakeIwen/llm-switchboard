"""The sessions to keep running, persisted as JSON.

The CLI and the web server both edit this file, so every change takes an
exclusive lock and replaces the file atomically. The supervisor only reads it.
"""

from dataclasses import dataclass, asdict
from pathlib import Path
import time

from .json_file import JsonFile
from .paths import STORE_FILE
from .session_id import is_session_id


@dataclass(frozen=True)
class Entry:
    session_id: str
    enabled: bool
    # Remote Control display name; empty means "use the session's title".
    name: str = ""
    updated_at: float = 0.0
    # Set for sessions created by this app, which have no transcript until
    # Claude Code writes one: where to start it and what to call it.
    cwd: str = ""
    title: str = ""


class Store:
    def __init__(self, path: Path = STORE_FILE):
        self.path = Path(path)
        self.file = JsonFile(self.path)

    def load(self) -> dict[str, Entry]:
        raw = self.file.load()
        entries = {}
        for sid, fields in raw.get("sessions", {}).items():
            if not is_session_id(sid):
                continue
            entries[sid] = Entry(
                session_id=sid,
                enabled=bool(fields.get("enabled")),
                name=str(fields.get("name", "")),
                updated_at=float(fields.get("updated_at", 0.0)),
                cwd=str(fields.get("cwd", "")),
                title=str(fields.get("title", "")),
            )
        return entries

    def set_enabled(self, session_id: str, enabled: bool) -> Entry:
        return self._update(session_id, enabled=enabled)

    def set_name(self, session_id: str, name: str) -> Entry:
        return self._update(session_id, name=name.strip())

    def create(self, session_id: str, cwd: str, title: str) -> Entry:
        """A new session to start in cwd, enabled from the outset."""
        return self._update(session_id, enabled=True, cwd=cwd, title=title)

    def remove(self, session_id: str) -> None:
        with self.file.locked():
            entries = self.load()
            if entries.pop(session_id, None) is not None:
                self._write(entries)

    def _update(self, session_id: str, **changes) -> Entry:
        if not is_session_id(session_id):
            raise ValueError(f"not a session id: {session_id!r}")
        with self.file.locked():
            entries = self.load()
            current = entries.get(session_id, Entry(session_id=session_id, enabled=False))
            fields = {**asdict(current), **changes, "updated_at": time.time()}
            entries[session_id] = Entry(**fields)
            self._write(entries)
            return entries[session_id]

    def _write(self, entries: dict[str, Entry]) -> None:
        payload = {
            "version": 1,
            "sessions": {
                sid: {k: v for k, v in asdict(e).items() if k != "session_id"}
                for sid, e in sorted(entries.items())
            },
        }
        self.file.write(payload)
