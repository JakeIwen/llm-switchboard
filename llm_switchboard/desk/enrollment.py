"""Which sessions the desk may route to (`enrolled.json`) and whether sends
are on (`desk-settings.json`). Both are small 0600 JSON files in the 0700
config dir, shared by the hub and the operator CLI through `JsonFile`'s lock.
They are read through `bounded_read` rather than `JsonFile.load`: the
supervisor reads `enrolled.json` on every launch, and a FIFO or a huge file
in its place must not stall its one thread (or an acknowledgement holding
the file's lock); either reads as unreadable.

Both files are opened and validated on every read, never answered from an
earlier one: a permission change or an in-place rewrite can leave a file's
mtime, size and inode as they were, and neither routing nor the sends switch
may outlive a read that fails. An `llm-switchboard desk
enroll|unenroll|sends|voice` from the CLI is seen at the hub's next recheck
without a restart. Anything unreadable reads as "nothing enrolled" and
"sends off": the desk fails closed, and says so once per break (below).
Editing is stricter: an
enroll or unenroll over an unreadable or malformed `enrolled.json` is
refused (503) rather than overwriting the sessions it may still hold.
`desk-settings.json` holds two switches, `sends_enabled` and
`voice_confirm_enabled`, each written by `desk sends|voice` through one
read-modify-write that keeps the other key (a broken file is replaced, and
then the other switch reads as off). Anything else in the file is dropped,
so it stays at most two boolean keys. An unreadable file means sends off and
voice off at the handoff, whose refresh reads it just before the target lock.

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
import stat
import threading
import weakref

from ..json_file import JsonFile
from ..session_id import is_session_id
from .bounded_read import read_bounded
from .model import MAX_DESK_FILE_BYTES, MAX_ENROLLED, DeskError
from .shallow_json import loads_shallow

log = logging.getLogger("llm_switchboard.desk")

# {version, sessions: {id: {since}}} nests three deep; a per-host file adds one.
MAX_DESK_FILE_DEPTH = 4


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Only when it differs: the hub takes the policy locks every 5 s, and a
    # needless chmod is still a metadata write on the SD card.
    if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
        os.chmod(path, 0o700)


class FileLock:
    """The exclusive flock on `<file>.lock` that `JsonFile.locked` takes (the
    same path, so the two exclude each other across processes), re-entrant
    within this process: other threads here wait on the mutex, other
    processes on the flock."""

    _instances: "weakref.WeakValueDictionary[str, FileLock]" = weakref.WeakValueDictionary()
    _instances_lock = threading.Lock()

    @classmethod
    def for_file(cls, path: Path) -> "FileLock":
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


class FailedReads:
    """One warning when a desk file's read starts failing (or fails in
    another way) and one line when it reads again, per file and per process,
    however often it is read in between. Every read is fresh, and the hub
    rereads these files every 5 s and at each handoff, the picker at each
    poll and the supervisor at each launch, each through readers of its own:
    a warning per read would grow the Pi's log on its SD card without bound
    while a file stays broken. Only files failing now are kept."""

    def __init__(self):
        self._failing: dict[str, str] = {}
        self._lock = threading.Lock()

    def failed(self, path: Path, how: str) -> None:
        key = os.path.abspath(path)
        with self._lock:
            if self._failing.get(key) == how:
                return
            self._failing[key] = how
        log.warning("desk: %s %s; treating it as empty", path.name, how)

    def read(self, path: Path) -> None:
        key = os.path.abspath(path)
        with self._lock:
            if self._failing.pop(key, None) is None:
                return
        log.info("desk: %s reads again", path.name)


# One per process, not per reader: the supervisor builds a reader per launch.
FAILED_READS = FailedReads()


class ValidatedJson:
    """A JsonFile opened, bounded and validated on every read (see the module
    docstring for why nothing is kept from an earlier read)."""

    def __init__(self, path: Path, valid=lambda data: True, lock_for: Path | None = None):
        self.file = JsonFile(path)
        # A file whose content fails `valid` reads as empty and can't be edited.
        self._valid = valid
        # Its own `<name>.lock`, or the lock of the file `lock_for` names.
        self.lock = FileLock.for_file(lock_for or path)

    def read_fresh(self) -> tuple[dict, bool]:
        """The content opened and validated now, and whether that read failed."""
        return self._load()

    def _load(self) -> tuple[dict, bool]:
        """The content, and whether it failed to read (then empty)."""
        try:
            data = self._read_json()
        except (OSError, ValueError, RecursionError):
            FAILED_READS.failed(self.file.path, "unreadable")
            return {}, True
        if not isinstance(data, dict) or not self._valid(data):
            FAILED_READS.failed(self.file.path, "malformed")
            return {}, True
        FAILED_READS.read(self.file.path)
        return data, False

    def _read_json(self):
        raw = read_bounded(self.file.path, MAX_DESK_FILE_BYTES)
        return {} if raw is None else loads_shallow(raw, MAX_DESK_FILE_DEPTH)

    def _load_for_edit(self) -> dict:
        try:
            data = self._read_json()
        except (OSError, ValueError, RecursionError) as exc:
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
            data = self._load()[0] if replace_unreadable else self._load_for_edit()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                # JsonFile writes through mkstemp, which creates the file 0600.
                self.file.write(data)


def valid_enrollment(data: dict) -> bool:
    """`{"sessions": {<session id>: {"since": <unix s>}}}` and nothing else, at
    most MAX_ENROLLED sessions. One bad entry makes the whole file malformed."""
    if not set(data) <= {"sessions"}:
        return False
    sessions = data.get("sessions", {})
    if not isinstance(sessions, dict) or len(sessions) > MAX_ENROLLED:
        return False
    return all(is_session_id(sid) and valid_since_record(record) for sid, record in sessions.items())


def valid_since_record(record) -> bool:
    if not isinstance(record, dict) or set(record) != {"since"}:
        return False
    since = record["since"]
    return isinstance(since, int) and not isinstance(since, bool) and since >= 0


class Enrollment:
    def __init__(self, path: Path):
        self._json = ValidatedJson(path, valid_enrollment)

    def locked(self):
        """Hold `enrolled.json`'s cross-process lock (re-entrant here)."""
        return self._json.lock.held()

    def sessions(self, *, strict: bool = False) -> dict[str, dict]:
        """The enrolled sessions, from the file opened and validated now (see
        the module docstring); an unreadable or malformed file reads as none,
        or raises (503) with `strict`, for callers that must not report a
        failed read as "nothing enrolled"."""
        data, unreadable = self._json.read_fresh()
        if unreadable and strict:
            raise DeskError(503, f"{self._json.file.path.name} unreadable")
        return dict(data.get("sessions", {}))

    def is_enrolled(self, session_id: str, *, strict: bool = False) -> bool:
        return session_id in self.sessions(strict=strict)

    def enroll(self, session_id: str, now: float) -> bool:
        """True when newly enrolled, False when it already was."""
        check_session_id(session_id)
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
        check_session_id(session_id)
        with self._json.updating() as data:
            sessions = data.get("sessions", {})
            if session_id not in sessions:
                return False
            del sessions[session_id]
            return True


SENDS_KEY = "sends_enabled"
VOICE_KEY = "voice_confirm_enabled"
SETTING_KEYS = (SENDS_KEY, VOICE_KEY)


class DeskSettings:
    def __init__(self, path: Path):
        self._json = ValidatedJson(path)

    def locked(self):
        """Hold `desk-settings.json`'s cross-process lock (re-entrant here)."""
        return self._json.lock.held()

    def sends_enabled(self) -> bool:
        """The master switch, from a read that succeeded now: a failed one
        is off."""
        return self._switch(SENDS_KEY)

    def voice_confirm_enabled(self) -> bool:
        """The voice gate, read the same way."""
        return self._switch(VOICE_KEY)

    def _switch(self, key: str) -> bool:
        data, unreadable = self._json.read_fresh()
        return not unreadable and data.get(key) is True

    def set_sends_enabled(self, enabled: bool) -> None:
        self._set(SENDS_KEY, enabled)

    def set_voice_confirm_enabled(self, enabled: bool) -> None:
        self._set(VOICE_KEY, enabled)

    def _set(self, key: str, enabled: bool) -> None:
        """One locked read-modify-write that keeps the other switch."""
        with self._json.updating(replace_unreadable=True) as data:
            kept = {k: data[k] for k in SETTING_KEYS if k != key and isinstance(data.get(k), bool)}
            data.clear()
            data.update(kept)
            data[key] = bool(enabled)


def check_session_id(session_id) -> None:
    if not isinstance(session_id, str) or not is_session_id(session_id):
        raise DeskError(400, "bad session_id")
