"""Talking to Codex's shared app-server daemon.

Codex keeps its sessions ("threads") in SQLite as well as in rollout files,
so the picker never touches those files; it asks the daemon, the same way the
Codex TUI and the ChatGPT apps do. The daemon speaks JSON-RPC over a WebSocket
on a Unix socket. This is a minimal client for that (standard library only):
one connection per batch of calls, text frames only.

The thread API is experimental and changes between Codex releases, so every
failure surfaces as CodexUnavailable or CodexError and the rest of the picker
carries on without Codex.
"""

from pathlib import Path
import base64
import contextlib
import json
import os
import socket
import struct

from .paths import CODEX_SOCKET

_TIMEOUT_S = 20
_CLIENT = {"name": "llm-switchboard", "version": "1"}


class CodexUnavailable(Exception):
    """The daemon isn't running or its socket can't be reached."""


class CodexError(Exception):
    """The daemon answered a call with an error."""


class CodexClient:
    def __init__(self, socket_path: Path | None = CODEX_SOCKET):
        self.socket_path = socket_path

    @property
    def enabled(self) -> bool:
        return self.socket_path is not None

    @contextlib.contextmanager
    def connect(self):
        """Yields an initialized _Connection; closes it afterwards."""
        if self.socket_path is None:
            raise CodexUnavailable("Codex support is turned off")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_TIMEOUT_S)
        try:
            sock.connect(str(self.socket_path))
        except OSError as e:
            sock.close()
            raise CodexUnavailable(f"the Codex daemon isn't reachable ({e.strerror or e}); "
                                   "`codex app-server daemon start` starts it") from e
        conn = _Connection(sock)
        try:
            conn.handshake()
            conn.call("initialize", {"clientInfo": _CLIENT, "capabilities": {"experimentalApi": True}})
            conn.notify("initialized")
            yield conn
        except (OSError, ValueError) as e:
            raise CodexUnavailable(f"lost the connection to the Codex daemon ({e})") from e
        finally:
            sock.close()

    def call(self, method: str, params: dict | None = None):
        with self.connect() as conn:
            return conn.call(method, params)


class _Connection:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""
        self.next_id = 0

    def handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ValueError(f"unexpected handshake reply: {status.decode(errors='replace')}")

    def call(self, method: str, params: dict | None = None):
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            msg = self._recv()
            # Notifications and other replies interleave; skip to ours.
            if msg.get("id") != self.next_id or "method" in msg:
                continue
            if "error" in msg:
                raise CodexError((msg["error"] or {}).get("message") or str(msg["error"]))
            return msg.get("result")

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _fill(self) -> None:
        data = self.sock.recv(65536)
        if not data:
            raise ValueError("connection closed")
        self.buf += data

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, obj, opcode: int = 0x1) -> None:
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        n = len(data)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        mask = os.urandom(4)  # clients must mask every frame
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self) -> dict:
        message = b""
        while True:
            b1, b2 = self._take(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            mask = self._take(4) if b2 & 0x80 else b""
            payload = self._take(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            opcode = b1 & 0x0F
            if opcode == 0x8:
                raise ValueError("the daemon closed the connection")
            if opcode == 0x9:
                self._send(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if b1 & 0x80:
                value = json.loads(message)
                return value if isinstance(value, dict) else {}
