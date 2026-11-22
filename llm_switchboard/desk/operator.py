"""The operator socket (`DeskConfig.operator_socket`, 0600 in a 0700 dir) and
its client, used by `llm-switchboard desk
pair|revoke|status|clear|sends|enroll|unenroll`.

One request per connection: the client writes one JSON line `{op, ...}`, the
hub answers one line `{ok, ...}` (`{ok: false, error}` on refusal) and
closes. A request line is at most `MAX_OPERATOR_LINE_BYTES`; responses are
bounded like the channel socket's lines (`protocol.read_line`). These are
operator messages, not channel `MessageType`s. At most
`MAX_OPERATOR_CONNECTIONS` are served at once; another is answered
`{ok: false, error: "busy"}` at once and closed, so a stuck client can't
pile up threads or stall `desk status` for longer than its own timeout.
"""

from enum import Enum
import contextlib
import json
import logging
import os
from pathlib import Path
import socket
import stat
import threading
from typing import Callable

from .enrollment import ensure_private_dir
from .model import MAX_OPERATOR_CONNECTIONS, MAX_OPERATOR_LINE_BYTES
from .protocol import FrameError, read_line

log = logging.getLogger("llm_switchboard.desk")

OPERATOR_TIMEOUT_SECONDS = 10
_ACCEPT_POLL_SECONDS = 0.5


class OperatorOp(str, Enum):
    PAIR = "pair"
    REVOKE = "revoke"
    STATUS = "status"
    CLEAR = "clear"
    SENDS = "sends"
    ENROLL = "enroll"
    UNENROLL = "unenroll"


class HubUnavailable(Exception):
    """No answer from a hub on the operator socket."""


class HubAbsent(HubUnavailable):
    """Positively no hub: no socket file, or nothing listens on it. Nothing
    was sent, so a policy write may be made to the file instead."""


class HubResultUnknown(HubUnavailable):
    """The request may have reached the hub, but no answer came back (EOF,
    reset, timeout, a garbled line): the hub may have applied it."""


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


class OperatorServer:
    def __init__(self, path: Path, handle: Callable[[dict], dict],
                 max_connections: int = MAX_OPERATOR_CONNECTIONS):
        self.path = Path(path)
        self._handle = handle
        self._slots = threading.BoundedSemaphore(max_connections)
        self._listener: socket.socket | None = None
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._listener = bind_private_socket(self.path)
        self._listener.settimeout(_ACCEPT_POLL_SECONDS)
        self._thread = threading.Thread(target=self._accept_loop, name="desk-operator", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._listener:
            self._listener.close()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                raise
            if not self._slots.acquire(blocking=False):
                self._refuse_busy(conn)
                continue
            try:
                threading.Thread(target=self._serve_one, args=(conn,), name="desk-operator-conn",
                                 daemon=True).start()
            except BaseException:
                self._slots.release()
                conn.close()
                raise

    @staticmethod
    def _refuse_busy(conn: socket.socket) -> None:
        """Answer without waiting on the client. Whatever it already sent is
        read first: closing over unread bytes resets the connection on Linux
        and the client would lose the answer."""
        with conn:
            conn.setblocking(False)
            with contextlib.suppress(OSError):
                conn.recv(MAX_OPERATOR_LINE_BYTES)
            _send(conn, {"ok": False, "error": "busy"})

    def _serve_one(self, conn: socket.socket) -> None:
        try:
            self._answer(conn)
        finally:
            self._slots.release()

    def _answer(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(OPERATOR_TIMEOUT_SECONDS)
            try:
                with conn.makefile("rb") as stream:
                    line = stream.readline(MAX_OPERATOR_LINE_BYTES + 1)
                if len(line) > MAX_OPERATOR_LINE_BYTES:
                    raise FrameError("line too long")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("not an object")
            except (OSError, FrameError, ValueError):
                _send(conn, {"ok": False, "error": "bad request"})
                return
            try:
                response = self._handle(message)
            except Exception:
                log.exception("desk: operator op %s failed", str(message.get("op"))[:32])
                response = {"ok": False, "error": "internal error"}
            _send(conn, response)


def _send(conn: socket.socket, message: dict) -> None:
    with contextlib.suppress(OSError):
        conn.sendall(json.dumps(message, separators=(",", ":")).encode() + b"\n")


RESULT_UNKNOWN = "result unknown; run `llm-switchboard desk status`"


def operator_request(path: Path, op: OperatorOp, timeout: float = OPERATOR_TIMEOUT_SECONDS, **fields) -> dict:
    """Send one operator request and return the hub's response. Raises
    `HubAbsent` only when the connect itself proved no hub listens,
    `HubResultUnknown` once connected without a whole answer, and plain
    `HubUnavailable` for any other connect failure (nothing was sent)."""
    data = json.dumps({"op": op.value, **fields}, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_OPERATOR_LINE_BYTES:
        raise ValueError("request too large")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise HubAbsent("the desk hub is not running") from exc
        except OSError as exc:
            raise HubUnavailable(f"the desk hub can't be reached ({type(exc).__name__}); nothing was sent") from exc
        line = _exchange(client, data)
    finally:
        client.close()
    try:
        response = json.loads(line) if line else None
    except ValueError:
        response = None
    if not isinstance(response, dict):
        raise HubResultUnknown(f"the desk hub gave no answer; {RESULT_UNKNOWN}")
    return response


def _exchange(client: socket.socket, data: bytes) -> bytes:
    """Write the request and read one answer line; b"" when none came."""
    try:
        # A busy hub may answer and close before reading; its answer still counts.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            client.sendall(data)
        with client.makefile("rb") as stream:
            return read_line(stream)
    except ConnectionResetError:
        return b""
    except (OSError, FrameError) as exc:
        raise HubResultUnknown(f"the desk hub stopped answering ({type(exc).__name__}); {RESULT_UNKNOWN}") from exc
