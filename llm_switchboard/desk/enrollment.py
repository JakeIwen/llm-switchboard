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

Every edit, the CLI's offline one included, holds the file's cross-process
lock (the `<name>.lock` flock `JsonFile.locked` takes) for its whole
read-modify-write. The hub holds both files' locks across rereading them,
publishing them and persisting a send's target lock, so an edit is either
seen by a handoff or waits for it. In this process the lock re-enters, so
the hub's own edits can run while it holds them. Order, everywhere: the
`enrolled.json` lock, then the `desk-settings.json` lock, then the hub's
policy lock, then the state's lock.
"""

import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import threading
import weakref

from ..json_file import JsonFile
from ..session_id import is_session_id
from .model import MAX_ENROLLED, DeskError

log = logging.getLogger("llm_switchboard.desk")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


class _FileLock:
    """The exclusive flock on `<file>.lock` that `JsonFile.locked` takes (the
    same path, so the two exclude each other across processes), re-entrant
    within this process: other threads here wait on the mutex, other
    processes on the flock."""

    _instances: "weakref.WeakValueDictionary[str, _FileLock]" = weakref.WeakValueDictionary()
    _instances_lock = threading.Lock()

    @classmethod
    def for_file(cls, path: Path) -> "_FileLock":
        lock_path = os.path.abspath(Path(path).with_suffix(".lock"))
        with cls._instances_lock:
            lock = cls._instances.get(lock_path)
            if lock is None:
                lock = cls._instances[lock_path] = cls(Path(lock_path))
            return lock

    def __init__(self, lock_path: Path):
        self.path = lock_path
        self._mutex = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    @contextlib.contextmanager
    def held(self):
        with self._mutex:
            if self._depth == 0:
                ensure_private_dir(self.path.parent)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    fd, self._fd = self._fd, None
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)


class _CachedJson:
    """A JsonFile whose parsed content is reused until the file changes."""

    def __init__(self, path: Path, valid=lambda data: True):
        self.file = JsonFile(path)
        # A file whose content fails `valid` reads as empty and can't be edited.
        self._valid = valid
        self._key = None
        self._data: dict = {}
        self.lock = _FileLock.for_file(path)

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
        if not isinstance(data, dict) or not self._valid(data):
            log.warning("desk: %s malformed; treating it as empty", self.file.path.name)
            return {}
        return data

    def _load_for_edit(self) -> dict:
        try:
            data = self.file.load()
        except (OSError, ValueError) as exc:
            raise DeskError(503, f"{self.file.path.name} unreadable; not changed") from exc
        if not isinstance(data, dict) or not self._valid(data):
            raise DeskError(503, f"{self.file.path.name} malformed; not changed")
        return data

    @contextlib.contextmanager
    def updating(self, replace_unreadable: bool = False):
        """Yields the current content for a read/modify/write under the lock.
        An unreadable file raises DeskError unless `replace_unreadable`."""
        ensure_private_dir(self.file.path.parent)
        with self.lock.held():
            data = self._load() if replace_unreadable else self._load_for_edit()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                # JsonFile writes through mkstemp, which creates the file 0600.
                self.file.write(data)
        self._key = None


def valid_enrollment(data: dict) -> bool:
    """`{"sessions": {<session id>: {"since": <unix s>}}}` and nothing else, at
    most MAX_ENROLLED sessions. One bad entry makes the whole file malformed."""
    if not set(data) <= {"sessions"}:
        return False
    sessions = data.get("sessions", {})
    if not isinstance(sessions, dict) or len(sessions) > MAX_ENROLLED:
        return False
    return all(is_session_id(sid) and _valid_record(record) for sid, record in sessions.items())


def _valid_record(record) -> bool:
    if not isinstance(record, dict) or set(record) != {"since"}:
        return False
    since = record["since"]
    return isinstance(since, int) and not isinstance(since, bool) and since >= 0


class Enrollment:
    def __init__(self, path: Path):
        self._json = _CachedJson(path, valid_enrollment)

    def locked(self):
        """Hold `enrolled.json`'s cross-process lock (re-entrant here)."""
        return self._json.lock.held()

    def sessions(self) -> dict[str, dict]:
        return dict(self._json.read().get("sessions", {}))

    def is_enrolled(self, session_id: str) -> bool:
        return session_id in self.sessions()

    def enroll(self, session_id: str, now: float) -> bool:
        """True when newly enrolled, False when it already was."""
        _check_session_id(session_id)
        with self._json.updating() as data:
            sessions = data.setdefault("sessions", {})
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
            if session_id not in sessions:
                return False
            del sessions[session_id]
            return True


class DeskSettings:
    def __init__(self, path: Path):
        self._json = _CachedJson(path)

    def locked(self):
        """Hold `desk-settings.json`'s cross-process lock (re-entrant here)."""
        return self._json.lock.held()

    def sends_enabled(self) -> bool:
        return self._json.read().get("sends_enabled") is True

    def set_sends_enabled(self, enabled: bool) -> None:
        with self._json.updating(replace_unreadable=True) as data:
            data.clear()
            data["sends_enabled"] = bool(enabled)


def _check_session_id(session_id) -> None:
    if not isinstance(session_id, str) or not is_session_id(session_id):
        raise DeskError(400, "bad session_id")
