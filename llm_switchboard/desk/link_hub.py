"""`LinkHub`: the hub's side of the Mac link (docs/voice-desk.md, "Hub
side", "Admission and proof of possession", "Deadlines, heartbeats and
backpressure", "Link objects, replacement and the fence on old work").

It owns `link.sock`: accept (2 admissions at once, 20 accepts a minute),
admission (`link_admission`), publication under the links lock (the
signer line reread, 6 admissions a minute per host, 4 hosts, replacement
of the host's current link), one reader thread per link (the admission
thread goes on as the reader) and one writer (`link_session`), the link
heartbeat and idle rule, presence queries, the signer check, the per-host
link records, the unattributed refusals, the link-wait caps, the
candidates (a `register` refused only as not enrolled, link_candidates),
the handoff gate and its count, and the link half of the rollback fence.

Lock rules: the links lock covers in-memory work, socket shutdowns and the
bounded read of `link-signers`; it is never held across link I/O or a wait
for it, and never while taking a policy lock or the state's lock.
Retiring is two parts: under the links lock the object is marked
`retired`, removed from the host table if it is the current one, and its
writer retired (socket shut down, unbegun frames `not_begun`); then,
outside it, its registrations are dropped through `Hub._drop_connection`
and its pending presence queries answer `unverifiable`. Logs carry codes,
host names once proven, session id prefixes and request ids of the hub's
shape, never content, labels or folders. Standard library only.
"""

from collections import deque
import contextlib
from dataclasses import dataclass
from enum import StrEnum
import logging
import os
import secrets
import socket
import threading
import time
from typing import Callable

from .labels import encodable
from .link_admission import (
    AdmissionClosed, AdmissionRefusals, AdmissionRefused, Admitted, admit, refusal_frame,
)
from .link_candidates import Candidates
from .link_fence import refuse_signers
from .link_proof import SignersUnreadable, read_signers, self_test, verify_proof
from .link_protocol import (
    ADMISSION_DEADLINE_SECONDS, HEARTBEAT_SECONDS, IDLE_TIMEOUT_SECONDS, LINK_ID_BYTES, MAX_ACCEPTS_PER_MINUTE,
    MAX_ADMISSIONS_PER_HOST_PER_MINUTE, MAX_LINK_HOSTS, MAX_LINK_HTTP_WAITS, MAX_LINK_LINE_BYTES,
    MAX_LINK_OPERATOR_WAITS, MAX_PENDING_ADMISSIONS, PRESENCE_DEADLINE_SECONDS, LinkFrameError, LinkType,
    PresenceState, RefusedCode, RegisterRefusedCode, Side, decode_frame,
)
from .link_session import LinkSession, RemoteChannel
from .link_views import LinkState
from .model import DeskError, RemoteIdentity, is_request_id
from .operator import bind_private_socket
from .peer_credentials import socket_peer_uid
from .refusals import Refusal

log = logging.getLogger("llm_switchboard.desk")

_ACCEPT_POLL_SECONDS = 0.5
_READ_POLL_SECONDS = 0.5
_TICK_SECONDS = 1.0
_READ_BYTES = 64 * 1024
_MINUTE = 60.0
# Hosts kept in the link records: every host with a signer line, a live
# link, or a link published in this run, the oldest down ones dropped first.
MAX_LINK_RECORDS = 16


class LinkWaitSurface(StrEnum):
    HTTP = "http"
    OPERATOR = "operator"


@dataclass
class _Record:
    up: bool
    since: float


class _RateLog:
    """At most one log line a minute per key."""

    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def due(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if now - self._last.get(key, -_MINUTE) < _MINUTE:
                return False
            self._last[key] = now
            return True


class _WaitSlot:
    def __init__(self, semaphore: threading.BoundedSemaphore):
        self._semaphore = semaphore

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._semaphore.release()
        return False


class LinkHub:
    def __init__(self, config, hub, *, verify=verify_proof, verifier_self_test=self_test,
                 peer_uid=socket_peer_uid, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], float] = time.time, nonce_bytes: Callable[[int], bytes] = secrets.token_bytes,
                 admission_seconds: float = ADMISSION_DEADLINE_SECONDS,
                 presence_seconds: float = PRESENCE_DEADLINE_SECONDS,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS, idle_seconds: float = IDLE_TIMEOUT_SECONDS,
                 tick_seconds: float = _TICK_SECONDS):
        """Reads nothing: `link-signers` is read from `start` on."""
        self._config = config
        self._hub = hub
        self._verify = verify
        self._self_test = verifier_self_test
        self._peer_uid = peer_uid
        self._clock = clock
        self._now = now
        self._nonce_bytes = nonce_bytes
        self.admission_seconds = admission_seconds
        self.presence_seconds = presence_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.idle_seconds = idle_seconds
        self._tick_seconds = tick_seconds
        self.verifier_available = False
        # The links lock (re-entrant: a writer's full queue retires its link
        # from inside a submit made under it).
        self._lock = threading.RLock()
        self._links: dict[str, LinkSession] = {}
        self._records: dict[str, _Record] = {}
        self._signer_hosts: frozenset[str] = frozenset()
        # Mac handoffs past the handoff gate and not yet out of it.
        self._handoffs = 0
        self._host_admissions: dict[str, deque[float]] = {}
        self._accepts: deque[float] = deque()
        self._pending = threading.BoundedSemaphore(MAX_PENDING_ADMISSIONS)
        self._waits = {LinkWaitSurface.HTTP: threading.BoundedSemaphore(MAX_LINK_HTTP_WAITS),
                       LinkWaitSurface.OPERATOR: threading.BoundedSemaphore(MAX_LINK_OPERATOR_WAITS)}
        self.refusals = AdmissionRefusals(now)
        self.candidates = Candidates(now)
        self._rate_log = _RateLog(clock)
        self._listener: socket.socket | None = None
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._frame_handlers = {
            LinkType.REGISTER: self._on_register, LinkType.UNREGISTERED: self._on_unregistered,
            LinkType.DELIVERED: self._on_delivered, LinkType.UNDELIVERABLE: self._on_undeliverable,
            LinkType.REPLY: self._on_reply, LinkType.PRESENCE: self._on_presence,
            LinkType.PING: self._on_ping, LinkType.PONG: self._on_pong,
        }

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        self.verifier_available = bool(self._self_test())
        if not self.verifier_available:
            log.warning("desk link: verifier self-test failed; Mac links disabled")
        self.check_signers()
        self._listener = bind_private_socket(self._config.link_socket)
        self._listener.settimeout(_ACCEPT_POLL_SECONDS)
        for target, name in ((self._accept, "desk-link-accept"), (self._ticker, "desk-link-tick")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._config.link_socket)
        with self._lock:
            links = list(self._links.values())
            for link in links:
                self._retire_locked(link, "hub stop")
        for link in links:
            self._finalize(link)
        for thread in self._threads:
            thread.join(timeout=5)

    # -- accept and admission ----------------------------------------------------------

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                sock, _ = self._listener.accept()
            except TimeoutError:
                continue
            except (OSError, AttributeError):
                return
            if not self._accept_allowed():
                if self._rate_log.due("accept"):
                    log.warning("desk link: connection closed at accept (admission caps)")
                sock.close()
                continue
            threading.Thread(target=self._serve, args=(sock,), name="desk-link", daemon=True).start()

    def _accept_allowed(self) -> bool:
        now = self._clock()
        while self._accepts and now - self._accepts[0] >= _MINUTE:
            self._accepts.popleft()
        if len(self._accepts) >= MAX_ACCEPTS_PER_MINUTE or not self._pending.acquire(blocking=False):
            return False
        self._accepts.append(now)
        return True

    def _serve(self, sock: socket.socket) -> None:
        link = None
        try:
            try:
                admitted = admit(sock, epoch=self._hub.epoch, signers_path=self._config.link_signers_file,
                                 verifier_available=self.verifier_available, verify=self._verify,
                                 peer_uid=self._peer_uid, nonce_bytes=self._nonce_bytes, clock=self._clock,
                                 deadline_seconds=self.admission_seconds)
                link = self._publish(admitted, sock)
            except AdmissionRefused as refused:
                self._refuse(sock, refused.code, refused.relay_versions)
            except AdmissionClosed as closed:
                if self._rate_log.due(f"closed:{closed.args[0]}"):
                    log.info("desk link: admission closed: %s", closed.args[0])
        finally:
            self._pending.release()
        if link is None:
            sock.close()
            return
        self._read(link, admitted)

    def _refuse(self, sock: socket.socket, code: RefusedCode, relay_versions=None) -> None:
        self.refusals.add(code, relay_versions)
        self._hub.state.set_admission_refusals(self.refusals.view())
        if self._rate_log.due(f"refused:{code.value}"):
            log.info("desk link: refused: %s", code.value)
        with contextlib.suppress(OSError):
            sock.settimeout(1.0)
            sock.sendall(refusal_frame(code))

    def _publish(self, admitted: Admitted, sock: socket.socket) -> LinkSession:
        """Step 7, one hold of the links lock: the signer line still carries
        the verified blob, the per-host rate, the host cap, replacement,
        then the new link and its `link_welcome`."""
        host = admitted.signer.host
        with self._lock:
            try:
                listed = {s.host: s for s in read_signers(self._config.link_signers_file)}
            except SignersUnreadable:
                raise AdmissionRefused(RefusedCode.SIGNERS_UNREADABLE) from None
            if listed.get(host) != admitted.signer:
                raise AdmissionRefused(RefusedCode.NOT_AUTHORIZED)
            times = self._host_admissions.setdefault(host, deque())
            now = self._clock()
            while times and now - times[0] >= _MINUTE:
                times.popleft()
            if len(times) >= MAX_ADMISSIONS_PER_HOST_PER_MINUTE:
                raise AdmissionRefused(RefusedCode.TOO_MANY_RECONNECTS)
            times.append(now)
            old = self._links.get(host)
            if old is None and len(self._links) >= MAX_LINK_HOSTS:
                raise AdmissionRefused(RefusedCode.TOO_MANY_HOSTS)
            if old is not None:
                self._retire_locked(old, "replaced")
            link = LinkSession(link_id=secrets.token_hex(LINK_ID_BYTES), signer=admitted.signer,
                               machine=admitted.machine, version=admitted.version, sock=sock, clock=self._clock,
                               retire=self.retire)
            self._links[host] = link
            self._set_record(host, up=True)
            link.writer.start()
            link.submit(LinkType.LINK_WELCOME, link_id=link.link_id, heartbeat_seconds=int(self.heartbeat_seconds),
                        idle_timeout_seconds=int(self.idle_seconds), max_line_bytes=MAX_LINK_LINE_BYTES)
        if old is not None:
            self._finalize(old)
        log.info("desk link: %s up (link %s)", host, link.link_id[:8])
        self.publish_views()
        return link

    # -- retirement ----------------------------------------------------------------

    def retire(self, link: LinkSession, reason: str) -> None:
        """Both parts, from any thread but a reader's own finalizer."""
        with self._lock:
            changed = self._retire_locked(link, reason)
        if changed:
            self._finalize(link)
            self.publish_views()

    def _retire_locked(self, link: LinkSession, reason: str) -> bool:
        if link.retired:
            return False
        link.retired = True
        if self._links.get(link.host) is link:
            del self._links[link.host]
            self._set_record(link.host, up=False)
        link.writer.retire()
        log.info("desk link: %s down (link %s): %s", link.host, link.link_id[:8], reason)
        return True

    def _finalize(self, link: LinkSession) -> None:
        """Outside the links lock: drop only this object's registrations."""
        for channel in link.finalize():
            self._hub._drop_connection(channel.conn_id, "link retired")

    def _set_record(self, host: str, *, up: bool) -> None:
        self._records[host] = _Record(up, self._now())
        if len(self._records) > MAX_LINK_RECORDS:
            spare = sorted((r.since, h) for h, r in self._records.items()
                           if not r.up and h not in self._signer_hosts)
            for _, old in spare[:len(self._records) - MAX_LINK_RECORDS]:
                del self._records[old]

    # -- the signer check ----------------------------------------------------------------

    def check_signers(self, host: str | None = None) -> dict:
        """Retire every link whose host has no line, or a line with another
        blob; an unreadable file retires every link. Returns, from the same
        hold of the links lock, `host`'s signer and live link fingerprints
        (`readable` False when the file couldn't be read)."""
        with self._lock:
            try:
                listed = {s.host: s for s in read_signers(self._config.link_signers_file)}
            except SignersUnreadable:
                listed = None
            stale = [link for link in self._links.values()
                     if listed is None or listed.get(link.host) is None or listed[link.host].blob != link.blob]
            for link in stale:
                self._retire_locked(link, "signer check")
            self._signer_hosts = frozenset(listed or ())
            signer = listed.get(host) if listed is not None and host else None
            live = self._links.get(host) if host else None
            answer = {"readable": listed is not None,
                      "signer": {"fingerprint": signer.fingerprint} if signer else None,
                      "link": {"fingerprint": live.fingerprint} if live else None}
        for link in stale:
            self._finalize(link)
        self.publish_views()
        return answer

    # -- views -----------------------------------------------------------------------------

    def publish_views(self) -> None:
        """The link records into the state (outside the links lock)."""
        with self._lock:
            records = {host: (record.up and host in self._links, record.since)
                       for host, record in self._records.items()}
            signers = self._signer_hosts
        hosts = set(records) | set(signers) | set(self._hub.remote_hosts())
        views = []
        for host in sorted(hosts):
            up, since = records.get(host, (False, None))
            state = LinkState.UP if up else LinkState.DOWN
            if not self.verifier_available:
                state = LinkState.VERIFIER_UNAVAILABLE
            views.append({"host": host, "state": state.value, "since": since, "signer": host in signers})
        self._hub.state.set_links(views)
        self.publish_candidates()

    def publish_candidates(self) -> None:
        """The candidates into the state (each pass expires the old ones)."""
        self._hub.state.set_candidates(self.candidates.view())

    # -- the link-wait caps ------------------------------------------------------------------

    def wait_slot(self, surface: LinkWaitSurface) -> _WaitSlot:
        """A slot to wait on a link, taken now or refused at once (before
        any handoff), released when the `with` block ends."""
        semaphore = self._waits[surface]
        if not semaphore.acquire(blocking=False):
            if surface is LinkWaitSurface.HTTP:
                raise DeskError(503, Refusal.LINK_BUSY)
            raise DeskError(503, Refusal.LINK_OPERATOR_BUSY)
        return _WaitSlot(semaphore)

    # -- what the hub asks -----------------------------------------------------------------

    def recheck(self, identity: RemoteIdentity) -> str | None:
        """None while the identity's link object is live and its host's
        current link, and the session still enrolled for that host."""
        with self._lock:
            link = self._links.get(identity.host)
            current = link is not None and not link.retired and link.link_id == identity.link_id
        if not current:
            return "link gone"
        if self._hub.remote_owner(identity.session_id) != identity.host:
            return "not enrolled"
        return None

    def link_for_lock(self, lock) -> LinkSession | None:
        """The host's current link when its key fingerprint and machine are
        the lock's, else None: presence from any other link doesn't count."""
        with self._lock:
            link = self._links.get(lock.host)
            if link is None or link.retired or link.fingerprint != lock.link_key or link.machine != lock.machine:
                return None
            return link

    def presence(self, link: LinkSession, channel_pid: int, channel_start: str) -> PresenceState:
        """Asked outside every lock, within the presence deadline."""
        state = link.query_presence(channel_pid, channel_start, self.presence_seconds)
        log.info("desk link: presence on %s: %s", link.host, state.value)
        return state

    def live_link(self, host: str) -> LinkSession | None:
        with self._lock:
            return self._links.get(host)

    # -- the handoff gate and the fence ---------------------------------------------------------

    @contextlib.contextmanager
    def handoff_gate(self, identity: RemoteIdentity):
        """Around a Mac handoff's reread and its lock write: entered only
        while the identity's link is its host's live, current link, counted
        under the links lock until the write is over (whole or failed). The
        links lock is held only to enter and to leave, never across the
        write, so the count and the live links read in one hold of it say
        whether a Mac handoff can still write to the lock file."""
        with self._lock:
            link = self._links.get(identity.host)
            if link is None or link.retired or link.link_id != identity.link_id:
                raise DeskError(409, Refusal.TARGET_CHANGED_NOT_SENT)
            self._handoffs += 1
        try:
            yield
        finally:
            with self._lock:
                self._handoffs -= 1

    def fence_check(self) -> None:
        """The link half of the rollback fence, in one hold of the links
        lock: `link-signers` readable with no line, no live link, no handoff
        past the gate. Reads and refuses; changes nothing."""
        with self._lock:
            try:
                listed = read_signers(self._config.link_signers_file)
            except SignersUnreadable:
                raise DeskError(409, Refusal.FENCE_SIGNERS_UNREADABLE) from None
            refuse_signers(listed)
            if self._links:
                raise DeskError(409, Refusal.FENCE_LINK_LIVE, {"hosts": sorted(self._links)})
            if self._handoffs:
                raise DeskError(409, Refusal.FENCE_HANDOFF)

    # -- the reader ------------------------------------------------------------------------

    def _read(self, link: LinkSession, admitted: Admitted) -> None:
        splitter = admitted.splitter
        reason = "eof"
        try:
            link.sock.settimeout(_READ_POLL_SECONDS)
            while not link.retired:
                try:
                    data = link.sock.recv(_READ_BYTES)
                except TimeoutError:
                    continue
                except OSError:
                    reason = "read failed"
                    break
                if not data:
                    break
                link.last_received = self._clock()
                for line in splitter.feed(data):
                    kind, fields = decode_frame(line, Side.RELAY, v=link.version)
                    handler = self._frame_handlers.get(kind)
                    if handler is None:
                        raise LinkFrameError(f"unexpected {kind.value}")
                    handler(link, fields)
                    if link.retired:
                        break
        except LinkFrameError as exc:
            reason = f"bad frame: {exc}"
        except Exception:
            log.exception("desk link: reader raised")
            reason = "reader error"
        finally:
            self.retire(link, reason)
            self._finalize(link)
            with contextlib.suppress(OSError):
                link.sock.close()

    def _violation(self, link: LinkSession, what: str) -> None:
        raise LinkFrameError(what)

    def _registered(self, link: LinkSession, chan: int) -> RemoteChannel | None:
        """The channel now registered under `chan`; None for a late frame on
        a `chan` this link saw. A `chan` it never saw closes the link."""
        if not link.chan_seen(chan):
            self._violation(link, "unknown chan")
        return link.channel(chan)

    def _on_register(self, link: LinkSession, fields: dict) -> None:
        chan, session_id = fields["chan"], fields["session_id"]
        if not link.claim_chan(chan):
            self._violation(link, "chan reused")
        code = None
        if not link.register_allowed():
            code = RegisterRefusedCode.TOO_MANY_REGISTERS
        elif not link.has_room():
            code = RegisterRefusedCode.TOO_MANY_REGISTRATIONS
        else:
            owner = self._hub.remote_owner(session_id)
            if self._hub.enrolled_on_two_hosts(session_id) or owner not in (None, link.host):
                code = RegisterRefusedCode.ENROLLED_ON_ANOTHER_HOST
            elif owner is None:
                code = RegisterRefusedCode.NOT_ENROLLED
        if code is None:
            generation = self._register(link, fields)
            if generation is None:
                code = RegisterRefusedCode.TOO_MANY_REGISTRATIONS
            else:
                link.submit(LinkType.REGISTERED, chan=chan, generation=generation)
                return
        if self._rate_log.due(f"register:{code.value}"):
            log.info("desk link: %s register refused: %s", link.host, code.value)
        link.submit(LinkType.REGISTER_REFUSED, chan=chan, reason=code.value)
        if code is RegisterRefusedCode.NOT_ENROLLED:
            # Refused only for not being enrolled: a session asking to join.
            self.candidates.seen(link.host, session_id, fields["label"], fields["cwd"])
            self.publish_candidates()

    def _register(self, link: LinkSession, fields: dict) -> int | None:
        identity = RemoteIdentity(host=link.host, link_id=link.link_id, chan=fields["chan"],
                                  channel_pid=fields["channel_pid"], channel_start=fields["channel_start"],
                                  claude_pid=fields["claude_pid"], claude_start=fields["claude_start"],
                                  session_id=fields["session_id"])
        channel = self._hub.attach_connection(
            lambda conn_id: RemoteChannel(conn_id, link, identity.chan, identity.session_id, fields["label"],
                                          fields["cwd"]))
        try:
            generation = self._hub.state.register(channel.conn_id, identity.session_id, identity, host=link.host,
                                                  link_key=link.fingerprint, machine=link.machine)
        except DeskError:
            channel.relay_closed = True  # the refusal tells the relay
            self._hub._drop_connection(channel.conn_id, "too many registrations")
            return None
        if not link.add_channel(channel):
            self._hub._drop_connection(channel.conn_id, "link retired")
            return None
        log.info("desk link: %s registered session %s generation %d", link.host, identity.session_id[:8],
                 generation)
        self._hub.state.set_label(identity.session_id, encodable(fields["label"]) or identity.session_id[:8],
                                  encodable(fields["cwd"]))
        return generation

    def _on_unregistered(self, link: LinkSession, fields: dict) -> None:
        channel = self._registered(link, fields["chan"])
        if channel is not None:
            channel.relay_closed = True
            self._hub._drop_connection(channel.conn_id, f"unregistered: {fields['reason']}")

    def _on_delivered(self, link: LinkSession, fields: dict) -> None:
        channel = self._registered(link, fields["chan"])
        if channel is not None and self._hub.state.mark_delivered(channel.conn_id, fields["request_id"]):
            log.info("desk: delivered %s", fields["request_id"])

    def _on_undeliverable(self, link: LinkSession, fields: dict) -> None:
        channel = self._registered(link, fields["chan"])
        if channel is not None:
            self._hub._handle_undeliverable(channel, {"request_id": fields["request_id"], "reason": fields["reason"]})

    def _on_reply(self, link: LinkSession, fields: dict) -> None:
        channel = self._registered(link, fields["chan"])
        if channel is not None and is_request_id(fields["request_id"]):
            self._hub._handle_reply(channel, {"request_id": fields["request_id"], "text": fields["text"],
                                              "spoken": fields["spoken"]})

    def _on_presence(self, link: LinkSession, fields: dict) -> None:
        if not link.answer_presence(fields["query_id"], PresenceState(fields["state"])):
            self._violation(link, "unknown query_id")

    def _on_ping(self, link: LinkSession, fields: dict) -> None:
        link.submit(LinkType.PONG, n=fields["n"])

    def _on_pong(self, link: LinkSession, fields: dict) -> None:
        if not link.pong_known(fields["n"]):
            self._violation(link, "pong for no ping")

    # -- heartbeats ----------------------------------------------------------------------

    def _ticker(self) -> None:
        while not self._stopping.wait(self._tick_seconds):
            try:
                self.tick()
            except Exception:
                log.exception("desk link: tick failed")

    def tick(self) -> None:
        """Ping after `heartbeat_seconds` without sending; retire after
        `idle_seconds` without a frame from the relay."""
        now = self._clock()
        with self._lock:
            links = list(self._links.values())
        for link in links:
            if now - link.last_received >= self.idle_seconds:
                self.retire(link, "idle")
            elif now - link.last_sent >= self.heartbeat_seconds:
                link.ping()
