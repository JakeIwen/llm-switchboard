"""The desk hub: one process wiring the state machine to the channel socket,
the operator socket and HTTPS (docs/voice-desk.md).

Channel connections: the hub reads the peer PID from the socket, reads
`hello`, has `peer.verify_peer` check the process chain, then answers
`welcome {epoch, generation}` or `refused {reason}` and closes. A verified
connection is rechecked before every dispatch and every 5 s; a failed
recheck or a disconnect drops its registration, and a request handed to it
becomes unknown (its lock stays), as does one the channel reports
`undeliverable`. Peer verification is injected (`peer=`) so tests run
without /proc; production imports `desk.peer` at start.

Logs carry ids and reasons only: a request id from the channel is logged
only when it has the hub-issued shape, never as sent.
"""

import contextlib
import itertools
import logging
import os
import re
import secrets
import signal
import socket
import threading
import time
from typing import Callable

from ..session_id import is_session_id
from .config import DeskConfig
from .enrollment import DeskSettings, Enrollment, ensure_private_dir
from .hub_state import HubState
from .http_server import DeskHttpServer, make_tls_context
from .labels import SessionLabels
from .locks import LockStore
from .model import (
    HELLO_TIMEOUT_SECONDS, MAX_PENDING_CHANNEL_CONNECTIONS, RECHECK_SECONDS, DeskError, is_request_id,
)
from .operator import OperatorOp, OperatorServer, bind_private_socket
from .protocol import (
    HEARTBEAT_SECONDS, IDLE_TIMEOUT_SECONDS, FrameError, MessageType, decode, encode, read_line,
)

log = logging.getLogger("llm_switchboard.desk")

_ACCEPT_POLL_SECONDS = 0.5
_MAX_LOGGED_REASON = 80
# A channel's own refusal reasons are short machine words; anything else is
# logged as a marker in case it carries text.
_LOGGABLE_REASON = re.compile(r"[A-Za-z0-9 _.:/-]{1,80}")
INVALID_ID_MARKER = "<invalid id>"


def _loggable(value) -> str:
    return value[:_MAX_LOGGED_REASON] if isinstance(value, str) else "?"


def _logged_request_id(value) -> str:
    return value if is_request_id(value) else INVALID_ID_MARKER


def _logged_channel_reason(value) -> str:
    return value if isinstance(value, str) and _LOGGABLE_REASON.fullmatch(value) else "?"


class ChannelConnection:
    def __init__(self, conn_id: int, sock: socket.socket, session_id: str):
        self.conn_id = conn_id
        self.sock = sock
        self.session_id = session_id
        self.last_sent = time.monotonic()
        self._write_lock = threading.Lock()

    def send(self, kind: MessageType, **fields) -> bool:
        try:
            data = encode(kind, **fields)
            with self._write_lock:
                self.sock.sendall(data)
                self.last_sent = time.monotonic()
            return True
        except (OSError, FrameError):
            return False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()


class Hub:
    def __init__(self, config: DeskConfig, *, peer=None, now: Callable[[], float] = time.time,
                 token_bytes: Callable[[int], bytes] = secrets.token_bytes, lock_store=None,
                 recheck_seconds: float = RECHECK_SECONDS, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 idle_timeout_seconds: float = IDLE_TIMEOUT_SECONDS, long_poll_seconds: float | None = None):
        self.config = config
        self._peer = peer
        self._now = now
        self.enrollment = Enrollment(config.enrolled_file)
        self.settings = DeskSettings(config.settings_file)
        self.state = HubState(lock_store or LockStore(config.lock_file), now=now, token_bytes=token_bytes)
        self.labels = SessionLabels(config)
        self.recheck_seconds = recheck_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        http_options = {} if long_poll_seconds is None else {"long_poll_seconds": long_poll_seconds}
        self.http = DeskHttpServer(config, self, **http_options)
        self.operator = OperatorServer(config.operator_socket, self.handle_operator)
        self._connections: dict[int, ChannelConnection] = {}
        self._connections_lock = threading.Lock()
        self._conn_ids = itertools.count(1)
        self._pending = threading.BoundedSemaphore(MAX_PENDING_CHANNEL_CONNECTIONS)
        self._channel_listener: socket.socket | None = None
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._operator_ops = {
            OperatorOp.PAIR: self._op_pair, OperatorOp.REVOKE: self._op_revoke,
            OperatorOp.STATUS: self._op_status, OperatorOp.CLEAR: self._op_clear,
            OperatorOp.SENDS: self._op_sends,
        }

    @property
    def epoch(self) -> str:
        return self.state.epoch

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        make_tls_context(self.config)
        if self._peer is None:
            from . import peer
            self._peer = peer
        for directory in (self.config.config_dir, self.config.state_dir, self.config.runtime_dir):
            ensure_private_dir(directory)
        self.refresh_files()
        self.refresh_labels()
        self._channel_listener = bind_private_socket(self.config.channel_socket)
        self._channel_listener.settimeout(_ACCEPT_POLL_SECONDS)
        try:
            self.operator.start()
            self.http.start()
        except BaseException:
            self.close()
            raise
        for target, name in ((self._accept_channels, "desk-channel-accept"), (self._housekeeping, "desk-recheck")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("desk hub up: epoch %s, https on 127.0.0.1:%d", self.epoch[:8], self.http.port)

    def close(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.state.close()
        self.http.close()
        self.operator.close()
        if self._channel_listener:
            self._channel_listener.close()
            self._channel_listener = None
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.config.channel_socket)
        with self._connections_lock:
            connections = list(self._connections.values())
        for connection in connections:
            connection.close()
        for thread in self._threads:
            thread.join(timeout=5)

    def serve(self) -> int:
        """Run until SIGTERM or SIGINT."""
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        self.start()
        try:
            while not stop.wait(1.0):
                pass
        finally:
            log.info("desk hub stopping")
            self.close()
        return 0

    # -- files and rechecks ----------------------------------------------------

    def refresh_files(self) -> None:
        self.state.set_enrolled(self.enrollment.sessions())
        self.state.set_sends_enabled(self.settings.sends_enabled())

    def refresh_labels(self) -> None:
        """Every target's label and folder, from bounded, cached transcript
        reads; never run by the state long-poll."""
        pids = {r.session_id: getattr(r.identity, "claude_pid", None) for r in self.state.registrations()}
        self.state.set_labels({session_id: self.labels.describe(session_id, pids.get(session_id))
                               for session_id in self.state.target_session_ids()})

    def recheck_now(self) -> None:
        """One pass of the 5 s loop: files, expiry, identity rechecks, labels."""
        self.refresh_files()
        self.state.expire()
        for registration in self.state.registrations():
            reason = self._recheck(registration.identity)
            if reason:
                self._drop_connection(registration.conn_id, f"recheck: {reason}")
        self.refresh_labels()

    def _recheck(self, identity) -> str | None:
        try:
            return self._peer.recheck(identity, self.config, self.enrollment.is_enrolled)
        except Exception:
            log.exception("desk: recheck raised")
            return "recheck error"

    def _housekeeping(self) -> None:
        while not self._stopping.wait(self.recheck_seconds):
            try:
                self.recheck_now()
                self._send_heartbeats()
            except Exception:
                log.exception("desk: housekeeping failed")

    def _send_heartbeats(self) -> None:
        now = time.monotonic()
        with self._connections_lock:
            connections = list(self._connections.values())
        for connection in connections:
            if now - connection.last_sent >= self.heartbeat_seconds and not connection.send(MessageType.PING):
                self._drop_connection(connection.conn_id, "heartbeat write failed")

    # -- channel connections ---------------------------------------------------

    def _accept_channels(self) -> None:
        while not self._stopping.is_set():
            try:
                sock, _ = self._channel_listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._pending.acquire(blocking=False):
                log.warning("desk: too many unverified channel connections; closing one")
                sock.close()
                continue
            threading.Thread(target=self._serve_channel, args=(sock,), name="desk-channel",
                             daemon=True).start()

    def _serve_channel(self, sock: socket.socket) -> None:
        conn_id = next(self._conn_ids)
        try:
            sock.settimeout(HELLO_TIMEOUT_SECONDS)
            stream = sock.makefile("rb")
            connection = self._admit(conn_id, sock, stream)
        finally:
            self._pending.release()
        if connection is None:
            with contextlib.suppress(OSError):
                stream.close()
            sock.close()
            return
        reason = "closed"
        try:
            sock.settimeout(self.idle_timeout_seconds)
            reason = self._read_channel(connection, stream)
        finally:
            with contextlib.suppress(OSError):
                stream.close()
            self._drop_connection(conn_id, reason)

    def _admit(self, conn_id: int, sock: socket.socket, stream) -> ChannelConnection | None:
        """hello -> verify -> register -> welcome, or refused and None."""
        refused = self._peer.PeerRefused
        try:
            pid = self._peer.socket_peer_pid(sock)
            kind, message = decode(read_line(stream))
            session_id = message.get("session_id")
            if kind is not MessageType.HELLO or not isinstance(session_id, str) or not is_session_id(session_id):
                raise refused("bad hello")
            identity = self._peer.verify_peer(pid, session_id, self.config, self.enrollment.is_enrolled)
            connection = ChannelConnection(conn_id, sock, session_id)
            with self._connections_lock:
                self._connections[conn_id] = connection
            generation = self.state.register(conn_id, session_id, identity)
        except (refused, DeskError) as exc:
            reason = exc.reason if isinstance(exc, DeskError) else str(exc.args[0] if exc.args else "refused")
            with self._connections_lock:
                self._connections.pop(conn_id, None)
            log.info("desk: channel refused: %s", _loggable(reason))
            with contextlib.suppress(OSError, FrameError):
                sock.sendall(encode(MessageType.REFUSED, reason=reason))
            return None
        except (FrameError, OSError):
            return None
        except Exception:
            log.exception("desk: channel verification raised")
            with self._connections_lock:
                self._connections.pop(conn_id, None)
            self.state.drop_registration(conn_id)
            return None
        if not connection.send(MessageType.WELCOME, epoch=self.epoch, generation=generation):
            self._drop_connection(conn_id, "welcome write failed")
            return None
        log.info("desk: channel registered: session %s generation %d", session_id[:8], generation)
        self.refresh_labels()
        return connection

    def _read_channel(self, connection: ChannelConnection, stream) -> str:
        while not self._stopping.is_set():
            try:
                line = read_line(stream)
                if not line:
                    return "disconnected"
                kind, message = decode(line)
            except FrameError as exc:
                return f"bad frame: {exc}"
            except TimeoutError:
                return "idle timeout"
            except OSError:
                return "read failed"
            if kind is MessageType.PING:
                connection.send(MessageType.PONG)
            elif kind is MessageType.PONG:
                pass
            elif kind is MessageType.DELIVERED:
                log.info("desk: delivered %s", _logged_request_id(message.get("request_id")))
            elif kind is MessageType.UNDELIVERABLE:
                self._handle_undeliverable(connection, message)
            elif kind is MessageType.REPLY:
                self._handle_reply(connection, message)
            else:
                return f"unexpected {kind.value}"
        return "hub stopping"

    def _handle_reply(self, connection: ChannelConnection, message: dict) -> None:
        request_id = message.get("request_id")
        reason = self.state.handle_reply(connection.conn_id, request_id, message.get("text"), message.get("spoken"))
        if reason:
            log.info("desk: reply for %s dropped: %s", _logged_request_id(request_id), reason)
        else:
            log.info("desk: reply for %s accepted", _logged_request_id(request_id))
        connection.send(MessageType.REPLY_RESULT, request_id=request_id if isinstance(request_id, str) else None,
                        ok=reason is None, reason=reason)

    def _handle_undeliverable(self, connection: ChannelConnection, message: dict) -> None:
        """The channel's check just before emitting failed. The request
        becomes unknown with its lock kept, and the connection is rechecked
        at once, since the usual cause (/clear, resume) also fails it."""
        request_id = message.get("request_id")
        changed = self.state.mark_undeliverable(connection.conn_id, request_id)
        log.info("desk: channel could not deliver %s (%s)%s", _logged_request_id(request_id),
                 _logged_channel_reason(message.get("reason")), "; now unknown" if changed else "")
        registration = next((r for r in self.state.registrations() if r.conn_id == connection.conn_id), None)
        reason = "connection gone" if registration is None else self._recheck(registration.identity)
        if reason:
            self._drop_connection(connection.conn_id, f"recheck after undeliverable: {reason}")

    def _drop_connection(self, conn_id: int, reason: str) -> None:
        with self._connections_lock:
            connection = self._connections.pop(conn_id, None)
        lost = self.state.drop_registration(conn_id)
        if connection:
            log.info("desk: channel %d dropped: %s", conn_id, _loggable(reason))
            connection.close()
        for request_id in lost:
            log.info("desk: request %s is now unknown", request_id)

    # -- the HTTP API (each takes the parsed body, and the protected ones the
    # bearer token, rechecked with the mutation; DeskError maps to a status) --

    def authenticate(self, token) -> bool:
        return self.state.authenticate(token)

    def wait_for_change(self, after: int, token: str, timeout: float) -> bool:
        return self.state.wait_for_change(after, token, timeout)

    def state_view(self) -> dict:
        return self.state.snapshot()

    def pair_start(self, body: dict) -> dict:
        return self.state.pair_start(body.get("secret_hash"))

    def pair_claim(self, body: dict) -> dict:
        return self.state.pair_claim(body.get("pair_id"), body.get("secret"))

    def ticket(self, body: dict, token: str) -> dict:
        with self.state.authorized(token):
            return self.state.issue_ticket(body.get("session_id"), body.get("generation"), body.get("epoch"))

    def send(self, body: dict, token: str) -> dict:
        request = self.state.begin_send(body.get("ticket"), body.get("text"), token)
        registration = next((r for r in self.state.registrations() if r.conn_id == request.conn_id), None)
        reason = "connection gone" if registration is None else self._recheck(registration.identity)
        if reason:
            self.state.expire_request(request.request_id)
            self._drop_connection(request.conn_id, f"dispatch recheck: {reason}")
            raise DeskError(409, "target changed; not sent")
        # A CLI unenroll or sends change since the last 5 s pass counts too.
        self.refresh_files()
        conn_id, content = self.state.hand_off(request.request_id)
        written = False
        try:
            with self._connections_lock:
                connection = self._connections.get(conn_id)
            written = connection is not None and connection.send(
                MessageType.EVENT, request_id=request.request_id, content=content)
        finally:
            self.state.finish_write(request.request_id, written)
        if not written:
            self._drop_connection(conn_id, "event write failed")
        log.info("desk: request %s handed off to session %s", request.request_id, request.session_id[:8])
        return {"request_id": request.request_id, "state": self.state.request_state(request.request_id)}

    def clear(self, body: dict, token: str) -> dict:
        with self.state.authorized(token):
            self.state.clear_lock(body.get("session_id"), body.get("lock_id"))
        return {"cleared": True}

    def enroll(self, body: dict, token: str) -> dict:
        session_id = body.get("session_id")
        with self.state.authorized(token):
            self.enrollment.enroll(session_id, self._now())
            self.refresh_files()
        return {"session_id": session_id, "enrolled": True}

    def unenroll(self, body: dict, token: str) -> dict:
        session_id = body.get("session_id")
        with self.state.authorized(token):
            self.enrollment.unenroll(session_id)
            self.refresh_files()
        # Routing stops at once, not at the next recheck.
        for registration in self.state.registrations():
            if registration.session_id == session_id:
                self._drop_connection(registration.conn_id, "unenrolled")
        return {"session_id": session_id, "enrolled": False}

    # -- the operator socket -----------------------------------------------------

    def handle_operator(self, message: dict) -> dict:
        try:
            op = OperatorOp(message.get("op"))
        except ValueError:
            return {"ok": False, "error": "unknown op"}
        try:
            return {"ok": True, **self._operator_ops[op](message)}
        except DeskError as exc:
            return {"ok": False, "error": exc.reason}

    def _op_pair(self, message: dict) -> dict:
        self.state.pair_confirm(message.get("code"))
        return {}

    def _op_revoke(self, message: dict) -> dict:
        token_id = message.get("token_id")
        if token_id is not None and not isinstance(token_id, str):
            raise DeskError(400, "bad token_id")
        return {"revoked": self.state.revoke(token_id)}

    def _op_status(self, message: dict) -> dict:
        return {**self.state.status(), "enrolled": sorted(self.enrollment.sessions())}

    def _op_clear(self, message: dict) -> dict:
        self.state.clear_lock(message.get("session_id"), message.get("lock_id"))
        return {"cleared": True}

    def _op_sends(self, message: dict) -> dict:
        enabled = message.get("enabled")
        if not isinstance(enabled, bool):
            raise DeskError(400, "enabled must be true or false")
        self.settings.set_sends_enabled(enabled)
        self.state.set_sends_enabled(enabled)
        return {"sends_enabled": enabled}
