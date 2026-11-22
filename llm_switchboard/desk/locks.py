"""The target lock file (`DeskConfig.lock_file`): crash-durable locks that keep
a target busy across hub restarts until its answer or Jacob's explicit clear.

Every change rewrites the whole file: temp file (0600) -> fsync -> rename ->
fsync the directory (0700), so a lock reported as added survives a crash.
Anything unexpected (unreadable, corrupt, over the cap, unwritable) raises
`LockStoreError`; the hub then refuses every send rather than guess.
"""

import contextlib
import json
import os
from pathlib import Path

from .model import MAX_LOCKS, Lock, LockStoreError

__all__ = ["LockStore", "LockStoreError"]

FILE_VERSION = 1
_FIELDS = {"lock_id": str, "host": str, "session_id": str, "request_id": str, "ticket_hash": str}


class LockStore:
    def __init__(self, path: Path, max_locks: int = MAX_LOCKS):
        self.path = Path(path)
        self.max_locks = max_locks
        self._locks: list[Lock] = []
        # Set when load failed: nothing may be written over a file we could not read.
        self._broken: str | None = None

    def load(self) -> list[Lock]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self._locks, self._broken = [], None
            return []
        except OSError as exc:
            self._broken = "lock file unreadable"
            raise LockStoreError(self._broken) from exc
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
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
            _fsync_dir(directory)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise LockStoreError("lock file unwritable") from exc


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse(raw: bytes, max_locks: int) -> list[Lock]:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
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
    if not isinstance(entry, dict):
        raise ValueError("entry not an object")
    for name, kind in _FIELDS.items():
        if not isinstance(entry.get(name), kind) or not entry[name]:
            raise ValueError(f"bad {name}")
    since = entry.get("since")
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        raise ValueError("bad since")
    return Lock(since=float(since), **{name: entry[name] for name in _FIELDS})
