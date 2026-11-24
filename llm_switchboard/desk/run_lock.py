"""Run locks: an empty file whose exclusive `flock` says one process of a
kind is alive (docs/voice-desk.md, "The run lock: what "no hub" means").

`hold_for_process` takes the lock for the rest of the calling process's
life: `desk serve` takes `hub.lock` before it constructs anything of the
hub, the relay takes `relay.lock`. The descriptor is opened `O_NOFOLLOW`
and `O_CLOEXEC` (no child inherits it, so none can outlive the process
holding it) and is never closed: the lock ends when the process exits,
which ends every thread it had. `probe` takes the same lock non-blocking
for one block, for a decision that rests on no such process existing
(the offline `link-fence`, `link-revoke` and `link-authorize` outcomes,
`relay-install --unload`'s confirmation); it never waits.

The file is created 0600 (its 0700 directory too) and is never written or
deleted. It is opened non-blocking and must be a regular file, so a FIFO or
a device in its place refuses at once instead of hanging the open.
Standard library only.
"""

import contextlib
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import stat
import time
from typing import Callable

from .enrollment import ensure_private_dir

# `desk serve` retries this long, longer than any command's hold.
START_WAIT_SECONDS = 5
POLL_SECONDS = 0.1

_OPEN_FLAGS = os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
# Descriptors held for the process's life; kept here so nothing, not even
# dropping the last reference to a HeldRunLock, closes one.
_HELD: list[int] = []


class RunLockHeld(Exception):
    """Another process holds the lock (args[0] is the lock's path)."""


class RunLockUnusable(OSError):
    """The lock file can't be opened as a regular file (a symlink, a FIFO,
    a directory, no permission); nothing can be decided from it."""


@dataclass(frozen=True)
class HeldRunLock:
    """Proof that this process holds the lock until it exits."""
    path: Path
    fd: int


def _open(path: Path) -> int:
    ensure_private_dir(path.parent)
    try:
        fd = os.open(path, _OPEN_FLAGS, 0o600)
    except OSError as exc:  # ELOOP for a symlink under O_NOFOLLOW
        raise RunLockUnusable(exc.errno, f"run lock {path} can't be opened: {exc.strerror}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RunLockUnusable(f"run lock {path} is not a regular file")
    return fd


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise
    return True


def hold_for_process(path: Path, *, wait_seconds: float = 0, poll_seconds: float = POLL_SECONDS,
                     clock: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep) -> HeldRunLock:
    """Take the lock non-blocking, retrying every `poll_seconds` for up to
    `wait_seconds`; RunLockHeld when it stays held. Never released."""
    path = Path(path)
    fd = _open(path)
    deadline = clock() + wait_seconds
    try:
        while not _try_lock(fd):
            if clock() >= deadline:
                raise RunLockHeld(str(path))
            sleep(poll_seconds)
    except BaseException:
        os.close(fd)
        raise
    _HELD.append(fd)
    return HeldRunLock(path, fd)


@contextlib.contextmanager
def probe(path: Path):
    """Hold the lock for the block, or raise RunLockHeld at once."""
    path = Path(path)
    fd = _open(path)
    try:
        if not _try_lock(fd):
            raise RunLockHeld(str(path))
        yield
    finally:
        os.close(fd)
