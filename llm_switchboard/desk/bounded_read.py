"""One bounded read of a small file the hub or the supervisor must never
block on: claude's `sessions/<pid>.json` for the startup acknowledgement,
and the desk's `enrolled.json` and `desk-settings.json`.

One open, never following a link and never blocking (a FIFO in the file's
place would otherwise stall the caller until something writes to it), then
fstat for a regular file and a read of at most `limit` bytes. Only a
definitive ENOENT is absence; every other failure raises OSError.
Standard library only.
"""

import errno
import os
from pathlib import Path
import stat

_OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def read_bounded(path: Path, limit: int) -> bytes | None:
    """The file's bytes, or None when it doesn't exist. Raises OSError when
    it can't be opened, isn't a regular file, or holds more than `limit`."""
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        if info.st_size > limit:
            raise OSError(errno.EFBIG, "file too large", str(path))
        data = _read_at_most(fd, limit + 1)
    finally:
        os.close(fd)
    # It may have grown between the fstat and the read.
    if len(data) > limit:
        raise OSError(errno.EFBIG, "file too large", str(path))
    return data


def _read_at_most(fd: int, limit: int) -> bytes:
    chunks, size = [], 0
    while size < limit:
        chunk = os.read(fd, limit - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)
