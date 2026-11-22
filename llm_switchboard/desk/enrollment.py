"""Which sessions the desk may route to (`enrolled.json`) and whether sends
are on (`desk-settings.json`). Both are small 0600 JSON files in the 0700
config dir, shared by the hub and the operator CLI through `JsonFile`'s lock.

The hub rereads them only when their mtime, size or inode changes, so an
`llm-switchboard desk enroll|unenroll|sends` from the CLI is seen at the
hub's next recheck without a restart. Anything unreadable reads as "nothing
enrolled" and "sends off": the desk fails closed. Editing is stricter: an
enroll or unenroll over an unreadable or malformed `enrolled.json` is
refused (503) rather than overwriting the sessions it may still hold.
`desk sends` rewrites its whole one-key file, so it replaces a broken one.
"""

import contextlib
import json
import logging
import os
from pathlib import Path

from ..json_file import JsonFile
from ..session_id import is_session_id
from .model import MAX_ENROLLED, DeskError

log = logging.getLogger("llm_switchboard.desk")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


class _CachedJson:
    """A JsonFile whose parsed content is reused until the file changes."""

    def __init__(self, path: Path):
        self.file = JsonFile(path)
        self._key = None
        self._data: dict = {}

    def read(self) -> dict:
        try:
            stat = self.file.path.stat()
            key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except FileNotFoundError:
            key = None
        if key != self._key or key is None:
            self._data = self._load()
            self._key = key
        return self._data

    def _load(self) -> dict:
        try:
            data = self.file.load()
        except (OSError, ValueError):
            log.warning("desk: %s unreadable; treating it as empty", self.file.path.name)
            return {}
        return data if isinstance(data, dict) else {}

    def _load_for_edit(self) -> dict:
        try:
            data = self.file.load()
        except (OSError, ValueError) as exc:
            raise DeskError(503, f"{self.file.path.name} unreadable; not changed") from exc
        if not isinstance(data, dict):
            raise DeskError(503, f"{self.file.path.name} malformed; not changed")
        return data

    @contextlib.contextmanager
    def updating(self, replace_unreadable: bool = False):
        """Yields the current content for a read/modify/write under the lock.
        An unreadable file raises DeskError unless `replace_unreadable`."""
        ensure_private_dir(self.file.path.parent)
        lock_path = self.file.path.with_suffix(".lock")
        os.close(os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600))
        with self.file.locked():
            data = self._load() if replace_unreadable else self._load_for_edit()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                # JsonFile writes through mkstemp, which creates the file 0600.
                self.file.write(data)
        self._key = None


class Enrollment:
    def __init__(self, path: Path):
        self._json = _CachedJson(path)

    def sessions(self) -> dict[str, dict]:
        sessions = self._json.read().get("sessions")
        if not isinstance(sessions, dict):
            return {}
        return {sid: info for sid, info in sessions.items() if isinstance(sid, str) and is_session_id(sid)}

    def is_enrolled(self, session_id: str) -> bool:
        return session_id in self.sessions()

    def enroll(self, session_id: str, now: float) -> bool:
        """True when newly enrolled, False when it already was."""
        _check_session_id(session_id)
        with self._json.updating() as data:
            sessions = data.setdefault("sessions", {})
            _check_sessions(sessions)
            if session_id in sessions:
                return False
            if len(sessions) >= MAX_ENROLLED:
                raise DeskError(409, "enrollment full")
            sessions[session_id] = {"since": int(now)}
            return True

    def unenroll(self, session_id: str) -> bool:
        """True when it was enrolled."""
        _check_session_id(session_id)
        with self._json.updating() as data:
            sessions = data.get("sessions", {})
            _check_sessions(sessions)
            if session_id not in sessions:
                return False
            del sessions[session_id]
            return True


class DeskSettings:
    def __init__(self, path: Path):
        self._json = _CachedJson(path)

    def sends_enabled(self) -> bool:
        return self._json.read().get("sends_enabled") is True

    def set_sends_enabled(self, enabled: bool) -> None:
        with self._json.updating(replace_unreadable=True) as data:
            data.clear()
            data["sends_enabled"] = bool(enabled)


def _check_sessions(sessions) -> None:
    if not isinstance(sessions, dict):
        raise DeskError(503, "enrolled.json malformed; not changed")


def _check_session_id(session_id) -> None:
    if not isinstance(session_id, str) or not is_session_id(session_id):
        raise DeskError(400, "bad session_id")
