"""The operator socket (`DeskConfig.operator_socket`, 0600 in a 0700 dir) and
its client, used by `llm-switchboard desk pair|revoke|status|clear|sends`.

One request per connection: the client writes one JSON line `{op, ...}`, the
hub answers one line `{ok, ...}` (`{ok: false, error}` on refusal) and
closes. Lines are bounded like the channel socket's (`protocol.read_line`),
but these are operator messages, not channel `MessageType`s.
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
from .protocol import FrameError, MAX_LINE_BYTES, read_line

log = logging.getLogger("llm_switchboard.desk")

OPERATOR_TIMEOUT_SECONDS = 10
_ACCEPT_POLL_SECONDS = 0.5


class OperatorOp(str, Enum):
    PAIR = "pair"
    REVOKE = "revoke"
    STATUS = "status"
    CLEAR = "clear"
    SENDS = "sends"


class HubUnavailable(Exception):
    """No hub answers on the operator socket."""


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
    def __init__(self, path: Path, handle: Callable[[dict], dict]):
        self.path = Path(path)
        self._handle = handle
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
            threading.Thread(target=self._serve_one, args=(conn,), name="desk-operator-conn",
                             daemon=True).start()

    def _serve_one(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(OPERATOR_TIMEOUT_SECONDS)
            try:
                with conn.makefile("rb") as stream:
                    message = json.loads(read_line(stream))
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


def operator_request(path: Path, op: OperatorOp, timeout: float = OPERATOR_TIMEOUT_SECONDS, **fields) -> dict:
    """Send one operator request and return the hub's response."""
    data = json.dumps({"op": op.value, **fields}, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_LINE_BYTES:
        raise ValueError("request too large")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise HubUnavailable("the desk hub is not running") from exc
        client.sendall(data)
        with client.makefile("rb") as stream:
            line = read_line(stream)
    finally:
        client.close()
    if not line:
        raise HubUnavailable("the desk hub closed the connection")
    response = json.loads(line)
    if not isinstance(response, dict):
        raise ValueError("bad response")
    return response
