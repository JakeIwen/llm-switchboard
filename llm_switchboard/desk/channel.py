"""The desk channel server: an MCP server over stdio (newline-delimited
JSON-RPC 2.0, written by hand so every Claude launch stays fast) plus a
client of the hub's channel socket.

Claude starts one per enrolled session. Events from the hub become
`notifications/claude/channel`; the `reply` tool sends Claude's answer back to
the hub. It never declares the permission-relay capability. Prompt and reply
text is never logged: stderr carries ids and reasons only.
Standard library only (`bin/desk-channel` runs it under `python -I`).
"""

import json
import os
import signal
import socket
import sys
import threading
import time

from . import protocol
from .config import DeskConfig
from .protocol import MessageType

VERSION = "0.1.0"
FALLBACK_PROTOCOL_VERSION = "2025-06-18"
REPLY_WAIT_SECONDS = 10
BACKOFF_FIRST_SECONDS = 1
BACKOFF_MAX_SECONDS = 30
# How long to wait for the hub's welcome or refusal after sending hello.
WELCOME_WAIT_SECONDS = 5
# Socket read granularity: bounds how late a heartbeat or idle check can run.
POLL_SECONDS = 0.5

REPLY_TOOL_SCHEMA = {
    "name": protocol.REPLY_TOOL,
    "description": "Send the answer to a voice desk message back to Jacob's desk. Call it exactly once per message.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "request_id from the channel tag"},
            "text": {"type": "string", "description": "your full answer"},
            "spoken": {"type": "string", "description": "one or two plain sentences summarizing the answer, to be read aloud"},
        },
        "required": ["request_id", "text", "spoken"],
    },
}


def log(message: str) -> None:
    print(f"desk-channel: {message}", file=sys.stderr, flush=True)


class StdoutWriter:
    """Events and responses come from different threads; one lock keeps lines whole."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, message: dict) -> bool:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        with self._lock:
            try:
                self._stream.write(data)
                self._stream.flush()
            except (OSError, ValueError):
                return False
        return True


class HubLink:
    """The socket to the hub: reconnects for as long as the process lives."""

    def __init__(self, config: DeskConfig, session_id: str, writer: StdoutWriter,
                 heartbeat_seconds: float = protocol.HEARTBEAT_SECONDS,
                 idle_timeout_seconds: float = protocol.IDLE_TIMEOUT_SECONDS):
        self._socket_path = str(config.channel_socket)
        self._session_id = session_id
        self._writer = writer
        self._heartbeat = heartbeat_seconds
        self._idle_timeout = idle_timeout_seconds
        self._lock = threading.Lock()  # guards _sock, _welcomed, _pending and sends
        self._sock: socket.socket | None = None
        self._welcomed = False
        self._pending: dict[str, dict] = {}

    # --- used by the MCP side ---

    def start(self) -> None:
        threading.Thread(target=self._run, name="hub-link", daemon=True).start()

    def send_reply(self, request_id: str, text: str, spoken: str) -> str | None:
        """None when the hub confirmed the delivery, else the reason it didn't."""
        waiter = {"done": threading.Event(), "ok": False, "reason": ""}
        with self._lock:
            if not self._welcomed or self._sock is None:
                return "not connected to the desk hub; the reply was not delivered"
            self._pending[request_id] = waiter
            sock = self._sock
        try:
            self._send(sock, MessageType.REPLY, request_id=request_id, text=text, spoken=spoken)
        except OSError:
            self._forget(request_id)
            return "not connected to the desk hub; the reply was not delivered"
        if not waiter["done"].wait(REPLY_WAIT_SECONDS):
            self._forget(request_id)
            return "the desk hub did not confirm the reply in time; delivery unconfirmed"
        self._forget(request_id)
        return None if waiter["ok"] else (waiter["reason"] or "the desk hub refused the reply")

    # --- connection loop ---

    def _forget(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def _send(self, sock: socket.socket, kind: MessageType, **fields) -> None:
        data = protocol.encode(kind, **fields)
        with self._lock:
            sock.sendall(data)

    def _run(self) -> None:
        backoff = BACKOFF_FIRST_SECONDS
        while True:
            welcomed = False
            try:
                welcomed = self._connect_and_serve()
            except (OSError, protocol.FrameError) as exc:
                log(f"hub link ended: {type(exc).__name__}")
            if welcomed:
                backoff = BACKOFF_FIRST_SECONDS
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)

    def _connect_and_serve(self) -> bool:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        welcomed = False
        try:
            sock.settimeout(WELCOME_WAIT_SECONDS)
            sock.connect(self._socket_path)
            sock.sendall(protocol.encode(MessageType.HELLO, session_id=self._session_id))
            reader = LineReader(sock)
            kind, message = protocol.decode(reader.read_line(WELCOME_WAIT_SECONDS))
            if kind is MessageType.REFUSED:
                log(f"hub refused this session: {str(message.get('reason', ''))[:80]}")
                return False
            if kind is not MessageType.WELCOME:
                log("hub answered hello with something else")
                return False
            with self._lock:
                self._sock, self._welcomed = sock, True
            welcomed = True
            log("connected to the desk hub")
            self._serve(sock, reader)
        finally:
            with self._lock:
                self._sock, self._welcomed = None, False
                waiting = list(self._pending.values())
                self._pending.clear()
            for waiter in waiting:
                waiter["reason"] = "connection to the desk hub dropped; delivery unconfirmed"
                waiter["done"].set()
            sock.close()
        return welcomed

    def _serve(self, sock: socket.socket, reader: "LineReader") -> None:
        last_rx = last_tx = time.monotonic()
        while True:
            now = time.monotonic()
            if now - last_rx > self._idle_timeout:
                log("hub link idle; reconnecting")
                return
            if now - last_tx >= self._heartbeat:
                self._send(sock, MessageType.PING)
                last_tx = now
            try:
                line = reader.read_line(POLL_SECONDS)
            except TimeoutError:
                continue
            last_rx = time.monotonic()
            kind, message = protocol.decode(line)
            self._handle(sock, kind, message)

    def _handle(self, sock: socket.socket, kind: MessageType, message: dict) -> None:
        if kind is MessageType.PING:
            self._send(sock, MessageType.PONG)
        elif kind is MessageType.EVENT:
            self._deliver_event(sock, message)
        elif kind is MessageType.REPLY_RESULT:
            with self._lock:
                waiter = self._pending.get(message.get("request_id"))
            if waiter is not None:
                waiter["ok"] = message.get("ok") is True
                waiter["reason"] = str(message.get("reason") or "")
                waiter["done"].set()

    def _deliver_event(self, sock: socket.socket, message: dict) -> None:
        request_id, content = message.get("request_id"), message.get("content")
        if not isinstance(request_id, str) or not isinstance(content, str):
            log("ignored a malformed event")
            return
        written = self._writer.send({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {"content": content, "meta": {"request_id": request_id}},
        })
        if not written:
            log(f"event {request_id} not written: stdout closed")
            return
        self._send(sock, MessageType.DELIVERED, request_id=request_id)


class LineReader:
    """Reads protocol lines from a socket with a deadline, never buffering past one line's cap."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buffer = b""

    def read_line(self, timeout: float) -> bytes:
        """Raises TimeoutError when no whole line arrived in time; OSError on close."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            if len(self._buffer) > protocol.MAX_LINE_BYTES:
                raise protocol.FrameError("line too long")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise TimeoutError from exc
            if not chunk:
                raise ConnectionResetError("hub closed the connection")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line


# --- MCP side ---------------------------------------------------------------


def initialize_result(params: dict) -> dict:
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    return {
        "protocolVersion": requested if isinstance(requested, str) and requested else FALLBACK_PROTOCOL_VERSION,
        # No claude/channel/permission: this channel never relays permission prompts.
        "capabilities": {"experimental": {"claude/channel": {}}, "tools": {}},
        "serverInfo": {"name": protocol.SERVER_NAME, "version": VERSION},
        "instructions": protocol.CHANNEL_INSTRUCTIONS,
    }


def tool_error(reason: str) -> dict:
    return {"content": [{"type": "text", "text": reason}], "isError": True}


def validate_reply_arguments(arguments) -> tuple[str, str, str] | str:
    """The three strings, or the reason the call is refused."""
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    values = [arguments.get(name) for name in ("request_id", "text", "spoken")]
    if not all(isinstance(value, str) for value in values):
        return "request_id, text and spoken must all be strings"
    request_id, text, spoken = values
    if not request_id:
        return "request_id is empty"
    if len(text.encode()) > protocol.MAX_REPLY_TEXT_BYTES:
        return f"text is over {protocol.MAX_REPLY_TEXT_BYTES} bytes; shorten it and call reply again"
    if len(spoken.encode()) > protocol.MAX_SPOKEN_BYTES:
        return f"spoken is over {protocol.MAX_SPOKEN_BYTES} bytes; shorten it and call reply again"
    return request_id, text, spoken


def call_reply(link: HubLink, arguments) -> dict:
    checked = validate_reply_arguments(arguments)
    if isinstance(checked, str):
        return tool_error(checked)
    request_id, text, spoken = checked
    reason = link.send_reply(request_id, text, spoken)
    if reason is not None:
        return tool_error(reason)
    return {"content": [{"type": "text", "text": "Delivered to Jacob's desk."}]}


class McpServer:
    def __init__(self, link: HubLink, writer: StdoutWriter):
        self._link = link
        self._writer = writer

    def handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return
        if "id" not in message:
            return  # notifications (initialized, cancelled, ...) need no answer
        threading.Thread(target=self._answer, args=(message,), daemon=True).start()

    def _answer(self, message: dict) -> None:
        request_id, method = message["id"], message["method"]
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            result = initialize_result(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [REPLY_TOOL_SCHEMA]}
        elif method == "tools/call":
            if params.get("name") != protocol.REPLY_TOOL:
                self._error(request_id, -32602, "unknown tool")
                return
            result = call_reply(self._link, params.get("arguments"))
        else:
            self._error(request_id, -32601, "method not found")
            return
        self._writer.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id, code: int, text: str) -> None:
        self._writer.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}})


def main() -> int:
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_: sys.exit(0))
    config = DeskConfig.from_env()
    writer = StdoutWriter(sys.stdout.buffer)
    link = HubLink(config, os.environ.get("CLAUDE_CODE_SESSION_ID", ""), writer)
    link.start()
    server = McpServer(link, writer)
    stdin = sys.stdin.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0
        server.handle_line(line)
