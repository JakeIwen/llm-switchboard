"""The desk hub: one process wiring the state machine to the channel socket,
the operator socket and HTTPS (docs/voice-desk.md).

Channel connections: the hub reads the peer PID from the socket, reads
`hello`, has `peer.verify_peer` check the process chain, then answers
`welcome {epoch, generation}` or `refused {reason}` and closes. A verified
connection is rechecked before every dispatch and every 5 s; a failed
recheck or a disconnect drops its registration, and a request handed to it
becomes unknown (its lock stays), as does one the channel reports
`undeliverable`. A lock whose delivery its channel never reported on (or
one from an earlier hub run) is cleared only once the channel process it
records is gone (`peer.process_presence`, from `config.proc_dir`). Peer verification is injected
(`peer=`) so tests run without /proc; production imports `desk.peer` at start.

Conversation mode (`grants.py`): the page asks for a grant, beats it every
5 s and ends it; a send confirmed by voice names it. The voice gate is read
fresh from `desk-settings.json` with every file refresh, including the one
just before each handoff, under the same locks as `sends`.

The startup acknowledgement (`startup_ack.py`) reads the dedicated tmux
server (`tmux=`, the supervisor's socket by default) and sends its one
Enter there; it takes only the enrollment file's lock and never the state's
across a tmux call (a revoke waits on `token_in_use` instead).

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
from ..tmux import Tmux
from .config import DeskConfig
from .enrollment import DeskSettings, Enrollment, ensure_private_dir
from .hub_enrollment import HubEnrollment
from .hub_link_ops import HubLinkOps
from .hub_state import HubState
from .http_server import DeskHttpServer, make_tls_context
from .labels import SessionLabels, encodable
from .link_hub import LinkHub, LinkWaitSurface
from .link_protocol import PRESENCE_PROCESS
from .locks import LockStore
from .model import (
    HELLO_TIMEOUT_SECONDS, HOST, MAX_PENDING_CHANNEL_CONNECTIONS, RECHECK_SECONDS,
    STARTUP_TMUX_TIMEOUT_SECONDS, ChannelProcess, ConfirmMode, DeskError, RemoteIdentity, ReplyRefusal,
    WriteOutcome, is_request_id,
)
from .operator import OperatorOp, OperatorServer, bind_private_socket
from .peer import process_presence
from .refusals import Refusal
from .remote_enrollment import HostEnrollment, routing_view
from .run_lock import HeldRunLock
from .protocol import (
    HEARTBEAT_SECONDS, IDLE_TIMEOUT_SECONDS, FrameError, MessageType, decode, encode, read_line,
)
from .startup_ack import StartupAck

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


# How each event write outcome reaches the request table: (written, attempted).
# Only not_begun lets a lock clear without a presence check.
_FINISH_WRITE = {
    WriteOutcome.WRITTEN: (True, True),
    WriteOutcome.NOT_BEGUN: (False, False),
    WriteOutcome.UNFINISHED: (False, True),
}


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

    def send_event(self, request_id: str, content: str) -> WriteOutcome:
        """A Pi channel's socket gives no byte counts back from `sendall`, so
        any failure may have sent part of the frame: unfinished."""
        if self.send(MessageType.EVENT, request_id=request_id, content=content):
            return WriteOutcome.WRITTEN
        return WriteOutcome.UNFINISHED

    def close(self, reason: str | None = None) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()


class Hub(HubEnrollment, HubLinkOps):
    def __init__(self, config: DeskConfig, *, peer=None, now: Callable[[], float] = time.time,
                 elapsed: Callable[[], float] = time.monotonic,
                 token_bytes: Callable[[int], bytes] = secrets.token_bytes, lock_store=None,
                 recheck_seconds: float = RECHECK_SECONDS, heartbeat_seconds: float = HEARTBEAT_SECONDS,
                 idle_timeout_seconds: float = IDLE_TIMEOUT_SECONDS, long_poll_seconds: float | None = None,
                 tmux: Tmux | None = None, link_options: dict | None = None):
        self.config = config
        self._peer = peer
        self._now = now
        self.enrollment = Enrollment(config.enrolled_file)
        self.settings = DeskSettings(config.settings_file)
        # Reading the policy files and publishing them into the state, and
        # changing them, happen under this lock so a stale read can't be
        # published over a newer change. Lock order, everywhere: the files'
        # cross-process locks (`_policy_files`), this lock, the state's.
        self._policy_lock = threading.RLock()
        # Mac enrollment, read only under enrolled.json's lock.
        self.host_enrollment = HostEnrollment(config.remote_enrolled_file, config.enrolled_file)
        # ({session id: host}, ids on two hosts, hosts in enrolled-hosts.json)
        # as last published, swapped whole: reader threads use it and never
        # read a policy file.
        self._published: tuple[dict[str, str], frozenset[str], frozenset[str]] = ({}, frozenset(), frozenset())
        self.state = HubState(lock_store or LockStore(config.lock_file), now=now, elapsed=elapsed,
                              token_bytes=token_bytes)
        self.labels = SessionLabels(config)
        self.startup = StartupAck(config, self.enrollment, tmux or Tmux(timeout=STARTUP_TMUX_TIMEOUT_SECONDS),
                                  self.state.epoch, now=now)
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
        self.links = LinkHub(config, self, **(link_options or {}))
        self._operator_ops = {
            OperatorOp.PAIR: self._op_pair, OperatorOp.REVOKE: self._op_revoke,
            OperatorOp.STATUS: self._op_status, OperatorOp.CLEAR: self._op_clear,
            OperatorOp.SENDS: self._op_sends, OperatorOp.VOICE: self._op_voice, OperatorOp.ENROLL: self._op_enroll,
            OperatorOp.UNENROLL: self._op_unenroll, OperatorOp.STARTUP_VIEW: self._op_startup_view,
            OperatorOp.STARTUP_ACK: self._op_startup_ack, OperatorOp.ENROLL_HOST: self._op_enroll_host,
            OperatorOp.UNENROLL_HOST: self._op_unenroll_host, OperatorOp.CLEAR_STOPPED: self._op_clear_stopped,
            OperatorOp.LINK_DROP: self._op_link_drop, OperatorOp.LINK_FENCE: self._op_link_fence,
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
            self.links.start()
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
        self.links.close()
        with self._connections_lock:
            connections = list(self._connections.values())
        for connection in connections:
            connection.close()
        for thread in self._threads:
            thread.join(timeout=5)

    def serve(self, run_lock: HeldRunLock) -> int:
        """Run until SIGTERM or SIGINT, holding `run_lock` (taken by `desk
        serve` before this hub was constructed; released only by the
        process's exit, which ends every thread still running a request)."""
        if not isinstance(run_lock, HeldRunLock):
            raise TypeError("a hub is served only under its held run lock")
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

    @contextlib.contextmanager
    def _policy_files(self):
        """Hold both policy files' cross-process locks, the ones the CLI's
        offline edit takes, in the fixed order: enrolled.json, then
        desk-settings.json. Re-entrant in this process."""
        with self.enrollment.locked(), self.settings.locked():
            yield

    def refresh_files(self) -> None:
        """Both enrollment files, read now under enrolled.json's lock,
        published as one view: an id with two owners is enrolled nowhere,
        and a file that fails this read routes nothing it governs."""
        with self._policy_files(), self._policy_lock:
            enrollment, conflicts, listed = routing_view(self.enrollment, self.host_enrollment)
            self._published = (enrollment, conflicts, listed)
            self.state.set_enrollment(enrollment, conflicts)
            self.state.set_sends_enabled(self.settings.sends_enabled())
            self.state.set_voice_enabled(self.settings.voice_confirm_enabled())

    def refresh_labels(self) -> None:
        """Every target's label and folder, from bounded, cached transcript
        reads; never run by the state long-poll. Never raises: a transcript
        that can't be read labels its target by the session id instead of
        stopping startup, a registration or the recheck loop. Both fields are
        made strict-UTF-8 encodable here too, since every snapshot carries them."""
        registrations = self.state.registrations()
        pids = {r.session_id: getattr(r.identity, "claude_pid", None) for r in registrations}
        # A Mac session's label and folder are what its link reported.
        with self._connections_lock:
            remote = {r.session_id: self._connections.get(r.conn_id) for r in registrations if r.host != HOST}
        labels = {}
        for session_id in self.state.target_session_ids():
            try:
                if session_id in remote or self.remote_owner(session_id) not in (HOST, None):
                    channel = remote.get(session_id)
                    label, cwd = ((channel.label or session_id[:8], channel.cwd) if channel
                                  else (session_id[:8], ""))
                else:
                    label, cwd = self.labels.describe(session_id, pids.get(session_id))
                labels[session_id] = (encodable(str(label)), encodable(str(cwd)))
            except Exception as exc:
                log.warning("desk: label for session %s failed: %s", session_id[:8], type(exc).__name__)
                labels[session_id] = (session_id[:8], "")
        self.state.set_labels(labels)

    def recheck_now(self) -> None:
        """One pass of the 5 s loop: the signer check, files, expiry,
        identity rechecks, labels."""
        self.links.check_signers()
        self.refresh_files()
        self.state.expire()
        for registration in self.state.registrations():
            reason = self._recheck(registration.identity)
            if reason:
                self._drop_connection(registration.conn_id, f"recheck: {reason}")
        self.refresh_labels()

    def _recheck(self, identity) -> str | None:
        try:
            if isinstance(identity, RemoteIdentity):
                return self.links.recheck(identity)
            return self._peer.recheck(identity, self.config, self.enrolled_on_pi)
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
            identity = self._peer.verify_peer(pid, session_id, self.config, self.enrolled_on_pi)
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
                request_id = message.get("request_id")
                if self.state.mark_delivered(connection.conn_id, request_id):
                    log.info("desk: delivered %s", _logged_request_id(request_id))
            elif kind is MessageType.UNDELIVERABLE:
                self._handle_undeliverable(connection, message)
            elif kind is MessageType.REPLY:
                self._handle_reply(connection, message)
            else:
                return f"unexpected {kind.value}"
        return "hub stopping"

    def _handle_reply(self, connection: ChannelConnection, message: dict) -> None:
        """A reply counts only while the connection still belongs to the
        session it registered as: after /clear the same channel process
        speaks for another session, so a failed recheck refuses the reply and
        drops the connection (the request becomes unknown, its lock stays)."""
        request_id = message.get("request_id")
        registration = next((r for r in self.state.registrations() if r.conn_id == connection.conn_id), None)
        recheck = self._recheck(registration.identity) if registration else None
        if recheck:
            log.info("desk: reply for %s refused: recheck: %s", _logged_request_id(request_id), _loggable(recheck))
            connection.send(MessageType.REPLY_RESULT, request_id=request_id if isinstance(request_id, str) else None,
                            ok=False, reason=ReplyRefusal.SESSION_CHANGED)
            self._drop_connection(connection.conn_id, f"recheck at reply: {recheck}")
            return
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

    def attach_connection(self, make):
        """A connection made by `make(conn_id)` under a fresh id from the
        hub's one counter, put in `_connections` (the link's proxies)."""
        connection = make(next(self._conn_ids))
        with self._connections_lock:
            self._connections[connection.conn_id] = connection
        return connection

    def remote_owner(self, session_id) -> str | None:
        """The host `session_id` is enrolled for as last published: HOST,
        a Mac host, or None (nowhere, or on two hosts)."""
        return self._published[0].get(session_id)

    def enrolled_on_two_hosts(self, session_id) -> bool:
        return session_id in self._published[1]

    def enrolled_on_pi(self, session_id) -> bool:
        """Phase 1's `is_enrolled` for a Pi channel: enrolled for `pi` in the view."""
        return self.remote_owner(session_id) == HOST

    def remote_hosts(self) -> set[str]:
        """The hosts enrolled-hosts.json lists sessions for."""
        return set(self._published[2])

    def _drop_connection(self, conn_id: int, reason: str) -> None:
        with self._connections_lock:
            connection = self._connections.pop(conn_id, None)
        lost = self.state.drop_registration(conn_id)
        if connection:
            log.info("desk: channel %d dropped: %s", conn_id, _loggable(reason))
            connection.close(reason)
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
        # A tab loaded before an install can't pair: its build isn't ours.
        if body.get("build") != self.http.page_build:
            raise DeskError(403, Refusal.PAGE_OUT_OF_DATE)
        return self.state.pair_claim(body.get("pair_id"), body.get("secret"))

    def ticket(self, body: dict, token: str) -> dict:
        with self.state.authorized(token):
            return self.state.issue_ticket(body.get("session_id"), body.get("generation"), body.get("epoch"),
                                           body.get("host"))

    def send(self, body: dict, token: str) -> dict:
        # Only an omitted `confirm` means a tap; a supplied null is refused.
        request = self.state.begin_send(body.get("ticket"), body.get("text"), token,
                                        body.get("confirm", ConfirmMode.TAP), body.get("grant"))
        try:
            # A Mac target's send waits on its link, within the link-wait cap.
            slot = (contextlib.nullcontext() if request.host == HOST
                    else self.links.wait_slot(LinkWaitSurface.HTTP))
        except BaseException:
            self.state.expire_request(request.request_id)
            raise
        with slot:
            return self._dispatch(request)

    def _dispatch(self, request) -> dict:
        try:
            conn_id, content = self._prepare_handoff(request)
        except BaseException:
            # Whatever failed before handoff, the pending request must not
            # keep the target busy (a no-op once it is no longer pending).
            self.state.expire_request(request.request_id)
            raise
        # Until a write has begun, an exception here proves nothing was sent.
        outcome = WriteOutcome.NOT_BEGUN
        try:
            with self._connections_lock:
                connection = self._connections.get(conn_id)
            if connection is not None:
                # Anything raised past this point may follow a partial write.
                outcome = WriteOutcome.UNFINISHED
                outcome = connection.send_event(request.request_id, content)
        finally:
            written, attempted = _FINISH_WRITE[outcome]
            self.state.finish_write(request.request_id, written, attempted=attempted)
        if not written:
            self._drop_connection(conn_id, "event write failed")
        log.info("desk: request %s (%s) handed off to session %s", request.request_id, request.confirm.value,
                 request.session_id[:8])
        return {"request_id": request.request_id, "state": self.state.request_state(request.request_id)}

    def _prepare_handoff(self, request) -> tuple[int, str]:
        registration = next((r for r in self.state.registrations() if r.conn_id == request.conn_id), None)
        reason = "connection gone" if registration is None else self._recheck(registration.identity)
        if reason:
            self.state.expire_request(request.request_id)
            self._drop_connection(request.conn_id, f"dispatch recheck: {reason}")
            raise DeskError(409, Refusal.TARGET_CHANGED_NOT_SENT)
        # A CLI unenroll or sends change since the last 5 s pass counts too,
        # and so does an enrollment file that no longer reads.
        # The file locks stay held until the target lock is persisted, so an
        # offline edit either lands before this reread or waits for the handoff.
        # A Mac target's handoff passes the link's handoff gate (counted until
        # its lock write is over, whole or failed): the rollback fence rests
        # on it, not on the recheck above.
        identity = registration.identity
        gate = (self.links.handoff_gate(identity) if isinstance(identity, RemoteIdentity)
                else contextlib.nullcontext())
        with self._policy_files(), self._policy_lock, gate:
            self.refresh_files()
            return self.state.hand_off(request.request_id)

    def conversation(self, body: dict, token: str) -> dict:
        grant = self.state.grant_conversation(token, body.get("session_id"), body.get("generation"),
                                              body.get("epoch"))
        log.info("desk: conversation granted for session %s", body["session_id"][:8])
        return grant

    def conversation_beat(self, body: dict, token: str) -> dict:
        return self.state.beat_grant(token, body.get("grant"))

    def conversation_end(self, body: dict, token: str) -> dict:
        ended = self.state.end_grant(token, body.get("grant"))
        log.info("desk: conversation ended by the page")
        return ended

    def clear(self, body: dict, token: str) -> dict:
        if not self.state.authenticate(token):
            raise DeskError(401, Refusal.UNAUTHORIZED)
        process = self._lock_presence(body.get("session_id"), body.get("lock_id"), LinkWaitSurface.HTTP)
        with self.state.authorized(token):
            self.state.clear_lock(body.get("session_id"), body.get("lock_id"), process)
        return {"cleared": True}

    def _channel_process(self, pid: int, start: str) -> ChannelProcess:
        return process_presence(self.config.proc_dir, pid, start)

    def _lock_presence(self, session_id, lock_id, surface: LinkWaitSurface) -> Callable[[int, str], ChannelProcess]:
        """The presence check `clear_lock` makes. For a Mac lock the answer
        is fetched first, outside every lock, from the host's current link
        only when its key and machine are the lock's, within the link-wait
        cap; `clear_lock` then uses that answer (`gone` is final)."""
        found = self.state.lock_info(session_id, lock_id)
        if found is None or found[0].host == HOST:
            return self._channel_process
        lock, needed = found
        if not needed:
            return lambda pid, start: ChannelProcess.UNVERIFIABLE
        link = self.links.link_for_lock(lock)
        if link is None:
            def not_connected(pid, start):
                raise DeskError(503, Refusal.MAC_NOT_CONNECTED)
            return not_connected
        with self.links.wait_slot(surface):
            answer = PRESENCE_PROCESS[self.links.presence(link, lock.channel_pid, lock.channel_start)]
        return lambda pid, start: answer

    def enroll(self, body: dict, token: str) -> dict:
        self._refuse_host_key(body)
        session_id = body.get("session_id")
        with self._policy_files(), self._policy_lock, self.state.authorized(token):
            self._enroll(session_id)
        return {"session_id": session_id, "enrolled": True}

    def unenroll(self, body: dict, token: str) -> dict:
        self._refuse_host_key(body)
        session_id = body.get("session_id")
        with self._policy_files(), self._policy_lock, self.state.authorized(token):
            self._unenroll(session_id)
        self._drop_unenrolled(session_id)
        return {"session_id": session_id, "enrolled": False}

    def startup_view(self, token: str) -> dict:
        """Enrolled sessions waiting at the startup warning (a protected read)."""
        return {"sessions": self.startup.blocked()}

    def startup_ack(self, body: dict, token: str) -> dict:
        """One Enter at the warning. It sends no prompt, so it doesn't depend
        on `sends`. The token is checked again after every other check, and
        a revoke of it waits until the Enter is out (`token_in_use`)."""
        return self.startup.acknowledge(body.get("session_id"), body.get("digest"),
                                        lambda: self.state.token_in_use(token))

    # -- the operator socket -----------------------------------------------------

    def handle_operator(self, message: dict) -> dict:
        try:
            op = OperatorOp(message.get("op"))
        except ValueError:
            return {"ok": False, "error": Refusal.UNKNOWN_OP}
        try:
            return {"ok": True, **self._operator_ops[op](message)}
        except DeskError as exc:
            return {"ok": False, "error": exc.reason, **exc.fields}

    def _op_pair(self, message: dict) -> dict:
        self.state.pair_confirm(message.get("code"))
        return {}

    def _op_revoke(self, message: dict) -> dict:
        token_id = message.get("token_id")
        if token_id is not None and not isinstance(token_id, str):
            raise DeskError(400, Refusal.BAD_TOKEN_ID)
        revoked = self.state.revoke(token_id)
        if not revoked.settled:
            raise DeskError(503, Refusal.REVOKE_IN_PROGRESS, {"revoked": revoked.count, "in_progress": True})
        return {"revoked": revoked.count}

    def _op_status(self, message: dict) -> dict:
        return {**self.state.status(), "enrolled": sorted(self.enrollment.sessions()),
                "enrolled_hosts": {host: sorted(ids) for host, ids in sorted(self.host_enrollment.hosts().items())}}

    def _op_clear(self, message: dict) -> dict:
        process = self._lock_presence(message.get("session_id"), message.get("lock_id"), LinkWaitSurface.OPERATOR)
        self.state.clear_lock(message.get("session_id"), message.get("lock_id"), process)
        return {"cleared": True}

    def _op_enroll(self, message: dict) -> dict:
        self._refuse_host_key(message)
        session_id = message.get("session_id")
        return {"session_id": session_id, "enrolled": True, "changed": self._enroll(session_id)}

    def _op_unenroll(self, message: dict) -> dict:
        self._refuse_host_key(message)
        session_id = message.get("session_id")
        removed = self._unenroll(session_id)
        self._drop_unenrolled(session_id)
        return {"session_id": session_id, "enrolled": False, "changed": removed}

    def _op_startup_view(self, message: dict) -> dict:
        return self.startup.view(message.get("session_id"))

    def _op_startup_ack(self, message: dict) -> dict:
        # The operator socket is 0600 and local: Jacob at the Pi's shell.
        return self.startup.acknowledge(message.get("session_id"), message.get("digest"), contextlib.nullcontext)

    def _op_sends(self, message: dict) -> dict:
        enabled = message.get("enabled")
        if not isinstance(enabled, bool):
            raise DeskError(400, Refusal.BAD_ENABLED)
        with self._policy_files(), self._policy_lock:
            self.settings.set_sends_enabled(enabled)
            self.state.set_sends_enabled(enabled)
        return {"sends_enabled": enabled}

    def _op_voice(self, message: dict) -> dict:
        enabled = message.get("enabled")
        if not isinstance(enabled, bool):
            raise DeskError(400, Refusal.BAD_ENABLED)
        with self._policy_files(), self._policy_lock:
            self.settings.set_voice_confirm_enabled(enabled)
            self.state.set_voice_enabled(self.settings.voice_confirm_enabled())
        return {"voice_confirm_enabled": enabled}
