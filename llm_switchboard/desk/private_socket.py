"""Private Unix socket binding shared by the Pi hub and Mac relay."""

import contextlib
import os
from pathlib import Path
import socket
import stat

from .enrollment import ensure_private_dir


class SocketInUse(Exception):
    """Another process already listens on the socket path."""


def bind_private_socket(path: Path, backlog: int = 16) -> socket.socket:
    """Listen on a 0600 Unix socket in a 0700 directory. A leftover socket
    file is removed only after a connect proves nothing listens on it."""
    path = Path(path)
    ensure_private_dir(path.parent)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if not stat.S_ISSOCK(mode):
            raise SocketInUse(f"{path.name} exists and is not a socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        else:
            raise SocketInUse(f"something already listens on {path.name}")
        finally:
            probe.close()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(backlog)
    except BaseException:
        listener.close()
        raise
    return listener
