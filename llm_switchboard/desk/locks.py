"""The target lock file (`DeskConfig.lock_file`): crash-durable locks that keep
a target busy across hub restarts until its answer or Jacob's explicit clear.
Each lock names the channel process (pid, start time) its event was handed
to, so a later hub run can tell whether that writer may still emit it.

Every change rewrites the whole file: temp file (0600) -> every byte
written -> fsync -> rename -> fsync the directory (0700), so a lock reported
as added survives a crash. Anything unexpected (unreadable, over
`MAX_LOCK_FILE_BYTES`, corrupt, an entry with a malformed field, over the
cap, unwritable) raises `LockStoreError`; the hub then refuses every send
rather than guess.
"""

import contextlib
import json
import math
import os
from pathlib import Path

from ..session_id import is_session_id
from .model import (
    CHANNEL_START_PATTERN, KNOWN_HOSTS, LOCK_ID_PATTERN, MAX_LOCK_FILE_BYTES, MAX_LOCKS, MAX_PID, SHA256_HEX_PATTERN,
    Lock, LockStoreError, is_request_id,
)
from .shallow_json import loads_shallow

__all__ = ["LockStore", "LockStoreError"]

FILE_VERSION = 1
# {version, locks: [{...}]} nests three deep.
MAX_LOCK_FILE_DEPTH = 4
_STRING_FIELDS = ("lock_id", "host", "session_id", "request_id", "ticket_hash", "channel_start")


class LockStore:
    def __init__(self, path: Path, max_locks: int = MAX_LOCKS):
        self.path = Path(path)
        self.max_locks = max_locks
        self._locks: list[Lock] = []
        # Set when load failed: nothing may be written over a file we could not read.
        self._broken: str | None = None

    def load(self) -> list[Lock]:
        try:
            with open(self.path, "rb") as f:
                raw = f.read(MAX_LOCK_FILE_BYTES + 1)
        except FileNotFoundError:
            self._locks, self._broken = [], None
            return []
        except OSError as exc:
            self._broken = "lock file unreadable"
            raise LockStoreError(self._broken) from exc
        if len(raw) > MAX_LOCK_FILE_BYTES:
            self._broken = "lock file too large"
            raise LockStoreError(self._broken)
        try:
            locks = _parse(raw, self.max_locks)
        except ValueError as exc:
            self._broken = f"lock file corrupt: {exc}"
            raise LockStoreError(self._broken) from exc
        self._locks, self._broken = locks, None
        return list(locks)

    def add(self, lock: Lock) -> None:
        """Returns only after the file holding `lock` is fsynced."""
        self._check_usable()
        try:
            _check_lock(lock)
        except ValueError as exc:
            raise LockStoreError(f"refusing a malformed lock: {exc}") from exc
        if len(self._locks) >= self.max_locks:
            raise LockStoreError("lock file full")
        if any(existing.lock_id == lock.lock_id for existing in self._locks):
            raise LockStoreError("duplicate lock id")
        updated = [*self._locks, lock]
        self._write(updated)
        self._locks = updated

    def remove(self, lock_id: str) -> None:
        self._check_usable()
        updated = [lock for lock in self._locks if lock.lock_id != lock_id]
        if len(updated) == len(self._locks):
            return
        self._write(updated)
        self._locks = updated

    def _check_usable(self) -> None:
        if self._broken:
            raise LockStoreError(self._broken)

    def _write(self, locks: list[Lock]) -> None:
        payload = json.dumps({"version": FILE_VERSION, "locks": [lock.to_json() for lock in locks]},
                             indent=1).encode() + b"\n"
        directory = self.path.parent
        tmp = directory / f".{self.path.name}.tmp"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            _fsync_dir(directory)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise LockStoreError("lock file unwritable") from exc


def _write_all(fd: int, data: bytes) -> None:
    """os.write may write less than asked (a full disk, a signal)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse(raw: bytes, max_locks: int) -> list[Lock]:
    try:
        data = loads_shallow(raw, MAX_LOCK_FILE_DEPTH)
    except ValueError as exc:
        raise ValueError("not JSON") from exc
    if not isinstance(data, dict) or data.get("version") != FILE_VERSION or not isinstance(data.get("locks"), list):
        raise ValueError("wrong shape")
    entries = data["locks"]
    if len(entries) > max_locks:
        raise ValueError("over the cap")
    locks = [_parse_lock(entry) for entry in entries]
    if len({lock.lock_id for lock in locks}) != len(locks):
        raise ValueError("duplicate lock id")
    return locks


def _parse_lock(entry) -> Lock:
    if not isinstance(entry, dict) or set(entry) != {*_STRING_FIELDS, "since", "channel_pid"}:
        raise ValueError("entry not a lock")
    since = entry["since"]
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        raise ValueError("bad since")
    try:
        since = float(since)
    except OverflowError:
        raise ValueError("bad since") from None
    lock = Lock(since=since, channel_pid=entry["channel_pid"], **{name: entry[name] for name in _STRING_FIELDS})
    _check_lock(lock)
    return lock


def _check_lock(lock: Lock) -> None:
    """Every field has the shape the hub writes; raises ValueError naming the first bad one."""
    checks = {
        "lock_id": lambda v: isinstance(v, str) and LOCK_ID_PATTERN.fullmatch(v),
        "host": lambda v: isinstance(v, str) and v in KNOWN_HOSTS,
        "session_id": lambda v: isinstance(v, str) and is_session_id(v),
        "request_id": is_request_id,
        "ticket_hash": lambda v: isinstance(v, str) and SHA256_HEX_PATTERN.fullmatch(v),
        "since": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0,
        "channel_pid": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 < v <= MAX_PID,
        "channel_start": lambda v: isinstance(v, str) and CHANNEL_START_PATTERN.fullmatch(v),
    }
    for name, valid in checks.items():
        if not valid(getattr(lock, name)):
            raise ValueError(f"bad {name}")
