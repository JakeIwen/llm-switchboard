"""The desk link's forced command on the Pi (`bin/desk-link`,
docs/voice-desk.md, "Admission and proof of possession", the pump).

sshd runs it for the Mac's desk key. It connects to the hub's `link.sock`
and copies whole lines both ways between its stdin/stdout (the SSH channel)
and that socket. It parses nothing: each line's length is checked against
MAX_LINK_LINE_BYTES before it is forwarded, and the hub is the one parser.
It doesn't authenticate whatever listens on `link.sock` (Who the design
defends against).

It exits on the first of:
- EOF (or a reset) from either side: exit 0. A partial line is never
  forwarded, so the receiver never sees a frame cut short.
- An oversize line from either side: exit 4. Whole lines before it are
  forwarded; nothing of it is.
- No hub to connect to: the version-free `link_refused {hub_not_running}`
  on stdout, exit 3.
and on exit writes one line `desk-link: <code>` to stderr, which ssh carries
back to the relay. The code is from PumpExit, never free text.

Each direction is one blocking thread, so a full peer blocks it: when ssh
stops reading, the pump stops reading the hub, and the hub's link writer
reaches its deadline. No arguments, no state, no log of its own. Standard
library only.
"""

from enum import Enum
import os
from pathlib import Path
import queue
import socket
import sys
import threading

from .config import DeskConfig
from .link_protocol import LINK_VERSIONS, LineSplitter, LinkFrameError, LinkType, RefusedCode, Side, encode_frame

READ_CHUNK_BYTES = 64 * 1024
# A hub that accepts nothing (a full backlog, a stopped process holding the
# socket) counts as no hub rather than holding the ssh session open.
CONNECT_TIMEOUT_SECONDS = 5
STDIN_FD = 0
STDOUT_FD = 1


class PumpExit(Enum):
    """Why the pump stopped: its stderr code and its exit status."""
    RELAY_CLOSED = ("relay_closed", 0)
    HUB_CLOSED = ("hub_closed", 0)
    HUB_NOT_RUNNING = ("hub_not_running", 3)
    OVERSIZE = ("oversize", 4)

    @property
    def code(self) -> str:
        return self.value[0]

    @property
    def status(self) -> int:
        return self.value[1]


def no_hub_frame() -> bytes:
    """The line the relay reads when no hub listens: the version-free
    `link_refused` shape, valid under every link version."""
    return encode_frame(LinkType.LINK_REFUSED, Side.HUB, reason=RefusedCode.HUB_NOT_RUNNING.value,
                        versions=list(LINK_VERSIONS))


def _pieces(data: bytes):
    """`data` cut after each newline, so lines before an oversize one in the
    same read are still forwarded."""
    start = 0
    while (end := data.find(b"\n", start)) >= 0:
        yield data[start:end + 1]
        start = end + 1
    if start < len(data):
        yield data[start:]


def _copy(read, write, closed: PumpExit) -> PumpExit:
    """Whole lines from `read` to `write` until EOF or an oversize line;
    `closed` names the side whose read or write failing ends the pump."""
    splitter = LineSplitter()
    while True:
        data = read()
        if not data:
            return closed  # a partial line held in the splitter is dropped
        lines = []
        try:
            for piece in _pieces(data):
                lines += splitter.feed(piece)
        except LinkFrameError:
            if lines:
                write(b"".join(lines))
            return PumpExit.OVERSIZE
        if lines:
            write(b"".join(lines))


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class _WriteFailed(Exception):
    pass


class _Direction:
    """One direction's reads and writes, each failure mapped to the side
    that failed: a read error is that side closing, and so is a write to a
    side that went away."""

    def __init__(self, read, write, read_closed: PumpExit, write_closed: PumpExit):
        self._read, self._write = read, write
        self._read_closed, self._write_closed = read_closed, write_closed

    def run(self) -> PumpExit:
        try:
            return _copy(self._safe_read, self._safe_write, self._read_closed)
        except _WriteFailed:
            return self._write_closed

    def _safe_read(self) -> bytes:
        try:
            return self._read()
        except OSError:
            return b""

    def _safe_write(self, data: bytes) -> None:
        try:
            self._write(data)
        except OSError:
            raise _WriteFailed from None


def pump(link_socket: Path, stdin_fd: int = STDIN_FD, stdout_fd: int = STDOUT_FD, err=None) -> int:
    """Run the pump between `stdin_fd`/`stdout_fd` and the hub at
    `link_socket`; the exit status. Writes the exit line to `err`
    (default: stderr)."""
    err = sys.stderr if err is None else err
    outcome = _run(Path(link_socket), stdin_fd, stdout_fd)
    try:
        err.write(f"desk-link: {outcome.code}\n")
        err.flush()
    except (OSError, ValueError):
        pass  # ssh already gone: nobody to tell
    return outcome.status


def _connect(path: Path) -> socket.socket | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_SECONDS)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        return None
    sock.settimeout(None)
    return sock


def _run(path: Path, stdin_fd: int, stdout_fd: int) -> PumpExit:
    sock = _connect(path)
    if sock is None:
        try:
            _write_all(stdout_fd, no_hub_frame())
        except OSError:
            pass
        return PumpExit.HUB_NOT_RUNNING
    directions = (
        _Direction(lambda: os.read(stdin_fd, READ_CHUNK_BYTES), sock.sendall,
              PumpExit.RELAY_CLOSED, PumpExit.HUB_CLOSED),
        _Direction(lambda: sock.recv(READ_CHUNK_BYTES), lambda data: _write_all(stdout_fd, data),
              PumpExit.HUB_CLOSED, PumpExit.RELAY_CLOSED),
    )
    outcomes: queue.Queue[PumpExit] = queue.Queue()
    running = [len(directions)]
    lock = threading.Lock()

    def run(direction: _Direction) -> None:
        outcomes.put(direction.run())
        with lock:
            running[0] -= 1
            last = running[0] == 0
        if last:
            sock.close()  # only once neither thread can still use it

    for direction in directions:
        threading.Thread(target=run, args=(direction,), daemon=True, name="desk-link-pump").start()
    first = outcomes.get()
    # The hub sees EOF at once. The other direction's thread ends with the
    # process (or, in a test, when its read side closes).
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    return first


def main() -> int:
    return pump(DeskConfig.for_launch().link_socket)
