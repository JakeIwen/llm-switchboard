"""The hub's in-memory state machine (docs/voice-desk.md, "Hub state").

No sockets, files, clock or randomness of its own: `now()` (the wall
clock, for timestamps and expiries), `elapsed()` (a clock no wall-clock
correction moves, for the conversation grant's lease) and `token_bytes(n)`
are injected, and the lock store is a collaborator with
`load()`, `add(lock)` (fsynced before it returns, raises `LockStoreError`)
and `remove(lock_id)`. One re-entrant mutex guards everything; a condition
on it plus the `version` counter lets long-polls sleep until something
visible changes.

A send runs in steps so the hub can recheck the channel's identity (I/O,
outside this module) between them: `begin_send` (pending) -> `hand_off`
(every routing rule checked again, the lock persisted, handed_off recorded
and the delivery marked in progress) -> the caller writes the event ->
`finish_write`; or `expire_request` when a check failed. The delivery stays
in progress (its lock can't be cleared) until that connection reports
`delivered` or `undeliverable` or answers: a socket write only means the
channel has the event, not that Claude does. A lock is cleared only on
positive evidence that no writer can still emit its event: that connection
reported on it, or nothing was ever written; otherwise (the connection went
away first, the write failed part way, or the lock is from an earlier hub
run) `clear_lock` asks the caller's `channel_process(pid, start)` about the
channel recorded in the lock, and only `GONE` clears. HTTP callers
run their mutation inside `authorized(token)`, so a token revoked while the
request was in flight can't act. The startup acknowledgement can't hold the
mutex across its tmux calls, so it runs them inside `token_in_use(token)`
instead. `revoke` waits (bounded) for every one still running on a token
it matches, valid or already deleted (so a second revoke during the first
one's wait waits too), and says whether they all finished: only a settled
revoke means no Enter can follow on that token.
"""

import base64
from collections import OrderedDict, deque
import contextlib
import hashlib
import hmac
import re
import threading
from typing import Callable

from .model import (
    CROCKFORD_ALPHABET, EPOCH_BYTES, HOST, LOCK_ID_BYTES, MAX_LOCKS, MAX_PAIR_SECRET_FAILURES,
    MAX_PAIR_STARTS_PER_MINUTE, MAX_PENDING_PAIRINGS, MAX_REGISTRATIONS, MAX_REPLIES,
    MAX_REPLY_RING_BYTES, MAX_REQUEST_AGE_SECONDS, MAX_REQUESTS, MAX_TICKETS, MAX_TOKENS,
    OUTSTANDING_STATES, PAIR_CODE_LENGTH,
    PAIR_ID_BYTES, PAIR_TTL_SECONDS, REQUEST_ID_RANDOM_BYTES, TICKET_RANDOM_BYTES, TICKET_TTL_SECONDS,
    STARTUP_REVOKE_WAIT_SECONDS, TOKEN_BYTES, TOKEN_ID_LENGTH, TOKEN_TTL_SECONDS, DeskError, Lock, LockStoreError, Pairing,
    ChannelProcess, Registration, RegistrationState, Reply, Request, RequestState, Revoked, Ticket, Token,
    valid_channel_identity,
)
from .grants import ConversationGrants
from .model import ConfirmMode
from .protocol import MAX_REPLY_TEXT_BYTES, MAX_SPOKEN_BYTES, event_content
from .refusals import Refusal
from .text_rule import check_text
from ..session_id import is_session_id

_HEX64 = re.compile(r"[0-9a-f]{64}")
_MAX_TOKEN_LENGTH = 128
_PAIR_RATE_WINDOW_SECONDS = 60
_CODE_FIXES = str.maketrans({"O": "0", "I": "1", "L": "1"})
# Their former names, kept for callers.
CHANNEL_STILL_RUNNING = Refusal.CHANNEL_STILL_RUNNING
CHANNEL_UNVERIFIABLE = Refusal.CHANNEL_UNVERIFIABLE


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class HubState(ConversationGrants):
    def __init__(self, lock_store, *, now: Callable[[], float], elapsed: Callable[[], float],
                 token_bytes: Callable[[int], bytes], epoch: str | None = None):
        # Re-entrant so `authorized()` can hold it around a method that takes it too.
        self._mutex = threading.RLock()
        self._changed = threading.Condition(self._mutex)
        self._now = now
        self._elapsed = elapsed
        self._token_bytes = token_bytes
        self.epoch = epoch or token_bytes(EPOCH_BYTES).hex()
        self._lock_store = lock_store
        self._version = 1
        self._closed = False
        self._generation = 0
        self._registrations: dict[int, Registration] = {}
        self._tickets: dict[str, Ticket] = {}
        self._requests: OrderedDict[str, Request] = OrderedDict()
        self._replies: OrderedDict[str, Reply] = OrderedDict()
        self._locks: dict[str, Lock] = {}
        self._lock_error: str | None = None
        self._sends_enabled = False
        self._enrolled: frozenset[str] = frozenset()
        self._pairings: dict[str, Pairing] = {}
        self._pair_starts: deque[float] = deque()
        self._tokens: dict[str, Token] = {}
        # Token hash -> acknowledgements authorized with it and still running,
        # kept apart from `_tokens` so they outlive the token's deletion.
        self._tokens_in_use: dict[str, int] = {}
        self.revoke_wait_seconds = STARTUP_REVOKE_WAIT_SECONDS
        # Request id -> connection, for events the channel hasn't reported
        # emitted or refused yet; their locks can't be cleared.
        self._in_delivery: dict[str, int] = {}
        # Request ids whose writer can't emit any more: the channel reported
        # delivered or undeliverable, or the event was never written. In
        # memory only, so an earlier run's lock always gets the process check.
        self._writer_done: set[str] = set()
        self._labels: dict[str, tuple[str, str]] = {}
        try:
            self._locks = {lock.lock_id: lock for lock in lock_store.load()}
        except LockStoreError as exc:
            self._lock_error = str(exc.args[0] if exc.args else "lock store error")

    # -- internal helpers (caller holds the mutex) ---------------------------

    def _bump(self) -> None:
        self._version += 1
        self._changed.notify_all()

    def _registrations_for(self, session_id: str) -> list[Registration]:
        return [r for r in self._registrations.values() if r.session_id == session_id]

    def _routable(self, session_id: str) -> Registration:
        found = self._registrations_for(session_id)
        if not found:
            raise DeskError(409, Refusal.NOT_REGISTERED)
        if len(found) > 1:
            raise DeskError(409, Refusal.AMBIGUOUS)
        return found[0]

    def _locks_for(self, session_id: str) -> list[Lock]:
        return sorted((lock for lock in self._locks.values() if lock.session_id == session_id),
                      key=lambda lock: lock.since)

    def _busy(self, session_id: str) -> bool:
        return bool(self._locks_for(session_id)) or any(
            r.session_id == session_id and r.state is RequestState.PENDING for r in self._requests.values())

    def _store_error(self) -> str | None:
        if self._lock_error:
            return self._lock_error
        return Refusal.LOCK_FILE_FULL if len(self._locks) >= MAX_LOCKS else None

    def _earlier_run(self, lock: Lock) -> bool:
        return not lock.request_id.startswith(self.epoch + ".")

    def _token_valid(self, token_hash: str | None) -> bool:
        token = self._tokens.get(token_hash or "")
        return token is not None and self._now() < token.expires_at

    def _finish(self, request: Request, state: RequestState) -> None:
        request.state = state
        request.finished = self._now()

    def _check_routing(self, session_id: str, *, sending: bool) -> None:
        """The rules every step of a send rechecks: sends on, a usable lock
        store, the target still enrolled."""
        if sending and not self._sends_enabled:
            raise DeskError(403, Refusal.SENDS_OFF)
        if self._store_error():
            raise DeskError(503, self._store_error())
        if session_id not in self._enrolled:
            raise DeskError(409, Refusal.NOT_ENROLLED)

    def _prune_history(self, now: float) -> bool:
        old = [request_id for request_id, r in self._requests.items()
               if r.state not in OUTSTANDING_STATES and r.finished is not None
               and now - r.finished >= MAX_REQUEST_AGE_SECONDS]
        for request_id in old:
            del self._requests[request_id]
            self._replies.pop(request_id, None)
        return bool(old)

    def _prune_tickets(self, now: float) -> None:
        for key in [k for k, t in self._tickets.items() if now >= t.expires_at]:
            del self._tickets[key]

    def _prune_pairings(self, now: float) -> None:
        for key in [k for k, p in self._pairings.items() if now >= p.expires_at]:
            del self._pairings[key]

    def _prune_tokens(self, now: float) -> bool:
        expired = [k for k, t in self._tokens.items() if now >= t.expires_at]
        for key in expired:
            del self._tokens[key]
        return bool(expired)

    # -- channel registrations ----------------------------------------------

    def register(self, conn_id: int, session_id: str, identity) -> int:
        """Record a verified channel connection; returns its generation."""
        with self._mutex:
            if len(self._registrations) >= MAX_REGISTRATIONS:
                raise DeskError(429, Refusal.TOO_MANY_REGISTRATIONS)
            self._generation += 1
            self._registrations[conn_id] = Registration(conn_id, session_id, self._generation, identity)
            self._bump()
            return self._generation

    def drop_registration(self, conn_id: int) -> list[str]:
        """The connection is gone or failed its recheck. Returns the ids of
        requests that became unknown (their locks stay)."""
        with self._mutex:
            if self._registrations.pop(conn_id, None) is None:
                return []
            # Unreported deliveries end without evidence: their locks get the process check.
            for request_id in [r for r, c in self._in_delivery.items() if c == conn_id]:
                del self._in_delivery[request_id]
            lost = []
            for request in self._requests.values():
                if request.conn_id != conn_id:
                    continue
                if request.state is RequestState.HANDED_OFF:
                    self._finish(request, RequestState.UNKNOWN)
                    lost.append(request.request_id)
                elif request.state is RequestState.PENDING:
                    self._finish(request, RequestState.EXPIRED)
            self._bump()
            return lost

    def registrations(self) -> list[Registration]:
        with self._mutex:
            return [Registration(r.conn_id, r.session_id, r.generation, r.identity, r.replied)
                    for r in self._registrations.values()]

    # -- tickets and sends ---------------------------------------------------

    def issue_ticket(self, session_id, generation, epoch) -> dict:
        if not isinstance(session_id, str) or not is_session_id(session_id):
            raise DeskError(400, Refusal.BAD_SESSION_ID)
        if not _is_int(generation) or not isinstance(epoch, str):
            raise DeskError(400, Refusal.BAD_GENERATION_OR_EPOCH)
        with self._mutex:
            if epoch != self.epoch:
                raise DeskError(409, Refusal.STALE_EPOCH)
            self._check_routing(session_id, sending=False)
            registration = self._routable(session_id)
            if registration.generation != generation:
                raise DeskError(409, Refusal.STALE_GENERATION)
            if self._busy(session_id):
                raise DeskError(409, Refusal.BUSY)
            now = self._now()
            self._prune_tickets(now)
            if len(self._tickets) >= MAX_TICKETS:
                raise DeskError(429, Refusal.TICKET_TABLE_FULL)
            ticket = f"{self.epoch}.{_b64url(self._token_bytes(TICKET_RANDOM_BYTES))}"
            record = Ticket(sha256_hex(ticket), session_id, generation, now + TICKET_TTL_SECONDS)
            self._tickets[record.ticket_hash] = record
            return {"ticket": ticket, "expires_at": record.expires_at}

    def begin_send(self, ticket, text, token, confirm=ConfirmMode.TAP, grant=None) -> Request:
        """Validate and consume the ticket; the request starts pending and
        remembers which token sent it, and how it was confirmed (`confirm`
        omitted is a tap, null is refused; a voice send names its `grant`,
        desk/grants.py)."""
        token_hash = self.token_hash(token)
        with self._mutex:
            if not self._token_valid(token_hash):
                raise DeskError(401, Refusal.UNAUTHORIZED)
            if not self._sends_enabled:
                raise DeskError(403, Refusal.SENDS_OFF)
            if self._store_error():
                raise DeskError(503, self._store_error())
            reason = check_text(text)
            if reason:
                raise DeskError(400, reason)
            record = self._ticket_for_send(ticket)
            mode, grant_id = self._admit_send_mode(confirm, grant, token_hash, record, text)
            self._check_routing(record.session_id, sending=True)
            registration = self._routable(record.session_id)
            if registration.generation != record.generation:
                raise DeskError(409, Refusal.STALE_GENERATION)
            if self._busy(record.session_id):
                raise DeskError(409, Refusal.BUSY)
            self._make_room_for_request()
            record.consumed = True
            request = Request(
                request_id=f"{self.epoch}.{self._token_bytes(REQUEST_ID_RANDOM_BYTES).hex()}",
                session_id=record.session_id, generation=record.generation,
                conn_id=registration.conn_id, text=text, created=self._now(),
                ticket_hash=record.ticket_hash, token_hash=token_hash, confirm=mode, grant_id=grant_id)
            self._requests[request.request_id] = request
            self._bump()
            return Request(**vars(request))

    def _ticket_for_send(self, ticket) -> Ticket:
        if not isinstance(ticket, str) or len(ticket) > 256:
            raise DeskError(400, Refusal.BAD_TICKET)
        epoch, dot, _ = ticket.partition(".")
        if not dot:
            raise DeskError(400, Refusal.BAD_TICKET)
        if epoch != self.epoch:
            raise DeskError(409, Refusal.STALE_EPOCH)
        record = self._tickets.get(sha256_hex(ticket))
        if record is None:
            raise DeskError(409, Refusal.UNKNOWN_TICKET)
        if self._now() >= record.expires_at:
            raise DeskError(409, Refusal.TICKET_EXPIRED)
        if record.consumed:
            raise DeskError(409, Refusal.TICKET_ALREADY_USED)
        return record

    def _make_room_for_request(self) -> None:
        if len(self._requests) < MAX_REQUESTS:
            return
        for request_id, request in self._requests.items():
            if request.state not in OUTSTANDING_STATES:
                del self._requests[request_id]
                self._replies.pop(request_id, None)
                return
        raise DeskError(429, Refusal.TOO_MANY_OUTSTANDING)

    def request_state(self, request_id: str) -> str | None:
        with self._mutex:
            request = self._requests.get(request_id)
            return request.state.value if request else None

    def expire_request(self, request_id: str) -> None:
        """The dispatch recheck failed before handoff: nothing was written."""
        with self._mutex:
            request = self._requests.get(request_id)
            if request and request.state is RequestState.PENDING:
                self._finish(request, RequestState.EXPIRED)
                self._bump()

    def hand_off(self, request_id: str) -> tuple[int, str]:
        """Recheck the sender's token and every routing rule, persist the
        target lock, record handed_off and mark the write in progress, then
        return the connection and event content the caller must now write
        (and report with `finish_write`). Any failed check expires the
        request: nothing was written."""
        with self._mutex:
            request = self._requests.get(request_id)
            if request is None or request.state is not RequestState.PENDING:
                raise DeskError(409, Refusal.REQUEST_NOT_PENDING)
            try:
                self._check_handoff(request)
            except DeskError:
                self._finish(request, RequestState.EXPIRED)
                self._bump()
                raise
            channel = self._registrations[request.conn_id].identity
            lock = Lock(lock_id=self._token_bytes(LOCK_ID_BYTES).hex(), host=HOST,
                        session_id=request.session_id, request_id=request.request_id,
                        ticket_hash=request.ticket_hash, since=self._now(),
                        channel_pid=channel.channel_pid, channel_start=channel.channel_start)
            try:
                self._lock_store.add(lock)
            except LockStoreError as exc:
                self._finish(request, RequestState.EXPIRED)
                reason = str(exc.args[0] if exc.args else "lock store error")
                if reason != "lock file full":
                    self._lock_error = reason
                self._bump()
                raise DeskError(503, reason) from exc
            self._recheck_voice_after_lock(request, lock)
            self._locks[lock.lock_id] = lock
            request.lock_id = lock.lock_id
            request.state = RequestState.HANDED_OFF
            self._in_delivery[request.request_id] = request.conn_id
            self._bump()
            return request.conn_id, event_content(request.text, request.request_id,
                                                  voice=request.confirm is ConfirmMode.VOICE)

    def _check_handoff(self, request: Request) -> None:
        if not self._token_valid(request.token_hash):
            raise DeskError(401, Refusal.UNAUTHORIZED)
        self._check_routing(request.session_id, sending=True)
        current = self._registrations.get(request.conn_id)
        others = self._registrations_for(request.session_id)
        if current is None or current.generation != request.generation or len(others) != 1:
            raise DeskError(409, Refusal.TARGET_CHANGED_NOT_SENT)
        # The lock must name the writer, or it could never be cleared safely.
        identity = current.identity
        if not valid_channel_identity(getattr(identity, "channel_pid", None), getattr(identity, "channel_start", None)):
            raise DeskError(409, Refusal.CHANNEL_IDENTITY_UNUSABLE)
        self._check_voice_handoff(request)

    def finish_write(self, request_id: str, written: bool, *, attempted: bool = True) -> None:
        """The caller's socket write is over. A failed write makes the request
        unknown; its lock stays. When the write was attempted, part of the
        event may have reached the channel, so its lock gets the process
        check; `attempted=False` (no connection to write to) is evidence
        nothing can emit it. A written event stays in delivery until the
        channel reports on it."""
        with self._mutex:
            request = self._requests.get(request_id)
            if not written:
                self._in_delivery.pop(request_id, None)
                if not attempted:
                    self._writer_done.add(request_id)
                if request and request.state is RequestState.HANDED_OFF:
                    self._finish(request, RequestState.UNKNOWN)
            self._bump()

    def _release_delivery(self, conn_id: int, request_id) -> bool:
        if not isinstance(request_id, str) or self._in_delivery.get(request_id) != conn_id:
            return False
        del self._in_delivery[request_id]
        self._writer_done.add(request_id)
        return True

    def mark_delivered(self, conn_id: int, request_id) -> bool:
        """The channel wrote the event to Claude's stdin. Only the connection
        it was handed to counts. True when a delivery ended."""
        with self._mutex:
            if not self._release_delivery(conn_id, request_id):
                return False
            self._bump()
            return True

    def mark_undeliverable(self, conn_id: int, request_id) -> bool:
        """The channel refused to emit the event (its own last check failed,
        e.g. /clear changed the session). Nothing reached Claude as far as
        the channel knows, but the hub can't prove it, so the request becomes
        unknown and keeps its lock. True when a request changed."""
        with self._mutex:
            request = self._requests.get(request_id) if isinstance(request_id, str) else None
            if request is None or request.conn_id != conn_id or request.state is not RequestState.HANDED_OFF:
                return False
            self._release_delivery(conn_id, request_id)
            self._finish(request, RequestState.UNKNOWN)
            self._bump()
            return True

    # -- replies -------------------------------------------------------------

    def handle_reply(self, conn_id: int, request_id, text, spoken) -> str | None:
        """None when this reply is accepted, else why it was dropped."""
        if not all(isinstance(value, str) for value in (request_id, text, spoken)):
            return "bad reply"
        # Strict UTF-8, before any state changes: a lone surrogate can't be
        # stored, served or sized, so it is refused here, not after the lock is gone.
        try:
            text_bytes, spoken_bytes = len(text.encode("utf-8")), len(spoken.encode("utf-8"))
        except UnicodeEncodeError:
            return "reply not valid UTF-8"
        if text_bytes > MAX_REPLY_TEXT_BYTES or spoken_bytes > MAX_SPOKEN_BYTES:
            return "reply too large"
        with self._mutex:
            request = self._requests.get(request_id)
            if request is None:
                return "unknown request"
            if request.conn_id != conn_id or conn_id not in self._registrations:
                return "wrong connection"
            if request.state is not RequestState.HANDED_OFF:
                return "request not outstanding"
            if request.lock_id and request.lock_id in self._locks:
                try:
                    self._lock_store.remove(request.lock_id)
                except LockStoreError as exc:
                    self._lock_error = str(exc.args[0] if exc.args else "lock store error")
                    self._bump()
                    return "lock store error"
                del self._locks[request.lock_id]
            self._release_delivery(conn_id, request_id)
            self._finish(request, RequestState.ANSWERED)
            self._registrations[conn_id].replied = True
            self._remember_reply(Reply(request_id, text, spoken))
            self._bump()
            return None

    def _remember_reply(self, reply: Reply) -> None:
        self._replies[reply.request_id] = reply
        while len(self._replies) > MAX_REPLIES or \
                sum(r.size for r in self._replies.values()) > MAX_REPLY_RING_BYTES:
            self._replies.popitem(last=False)

    # -- locks ----------------------------------------------------------------

    def clear_lock(self, session_id, lock_id,
                   channel_process: Callable[[int, str], ChannelProcess] | None = None) -> None:
        """Jacob checked the session: release exactly the lock he saw, only
        when nothing can still emit its event. Refused while the delivery is
        in progress; without a report or proof nothing was written (and for
        every earlier run's lock), cleared only when `channel_process(pid,
        start)` for the lock's channel says `GONE`: 409 while it runs, 503
        when it can't be checked or no checker is given. The check is a short
        /proc read, done under the state's lock."""
        if not isinstance(session_id, str) or not isinstance(lock_id, str):
            raise DeskError(400, Refusal.BAD_LOCK_REF)
        with self._mutex:
            locks = self._locks_for(session_id)
            if not locks:
                raise DeskError(409, Refusal.NO_LOCK)
            lock = next((lock for lock in locks if lock.lock_id == lock_id), None)
            if lock is None:
                raise DeskError(409, Refusal.LOCK_ID_MISMATCH)
            if lock.request_id in self._in_delivery:
                raise DeskError(409, Refusal.DELIVERY_IN_PROGRESS)
            if lock.request_id not in self._writer_done:
                seen = (channel_process(lock.channel_pid, lock.channel_start) if channel_process
                        else ChannelProcess.UNVERIFIABLE)
                if seen is ChannelProcess.RUNNING:
                    raise DeskError(409, Refusal.CHANNEL_STILL_RUNNING)
                if seen is not ChannelProcess.GONE:
                    raise DeskError(503, Refusal.CHANNEL_UNVERIFIABLE)
            try:
                self._lock_store.remove(lock.lock_id)
            except LockStoreError as exc:
                self._lock_error = str(exc.args[0] if exc.args else "lock store error")
                self._bump()
                raise DeskError(503, self._lock_error) from exc
            del self._locks[lock.lock_id]
            self._writer_done.discard(lock.request_id)
            request = self._requests.get(lock.request_id)
            # Nobody waits for it any more, so a late reply is dropped.
            if request and request.state is RequestState.HANDED_OFF:
                self._finish(request, RequestState.UNKNOWN)
            self._bump()

    # -- settings fed by the hub ---------------------------------------------

    def set_sends_enabled(self, enabled: bool) -> None:
        with self._mutex:
            if self._sends_enabled != bool(enabled):
                self._sends_enabled = bool(enabled)
                self._bump()

    def set_enrolled(self, session_ids) -> None:
        enrolled = frozenset(session_ids)
        with self._mutex:
            if enrolled != self._enrolled:
                self._enrolled = enrolled
                self._bump()

    def set_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        """Each target's (label, cwd), worked out by the hub off the request path."""
        labels = dict(labels)
        with self._mutex:
            if labels != self._labels:
                self._labels = labels
                self._bump()

    def target_session_ids(self) -> set[str]:
        with self._mutex:
            return self._target_ids()

    # -- pairing and tokens --------------------------------------------------

    def pair_start(self, secret_hash) -> dict:
        if not isinstance(secret_hash, str) or not _HEX64.fullmatch(secret_hash):
            raise DeskError(400, Refusal.BAD_SECRET_HASH)
        with self._mutex:
            now = self._now()
            while self._pair_starts and now - self._pair_starts[0] >= _PAIR_RATE_WINDOW_SECONDS:
                self._pair_starts.popleft()
            if len(self._pair_starts) >= MAX_PAIR_STARTS_PER_MINUTE:
                raise DeskError(429, Refusal.TOO_MANY_PAIRING_ATTEMPTS)
            self._pair_starts.append(now)
            self._prune_pairings(now)
            if len(self._pairings) >= MAX_PENDING_PAIRINGS:
                raise DeskError(429, Refusal.TOO_MANY_PENDING_PAIRINGS)
            code = self._new_code()
            pairing = Pairing(self._token_bytes(PAIR_ID_BYTES).hex(), code, secret_hash,
                              now + PAIR_TTL_SECONDS)
            self._pairings[pairing.pair_id] = pairing
            return {"pair_id": pairing.pair_id, "code": f"{code[:3]}-{code[3:]}",
                    "expires_in": PAIR_TTL_SECONDS}

    def _new_code(self) -> str:
        taken = {p.code for p in self._pairings.values()}
        while True:
            code = "".join(CROCKFORD_ALPHABET[b % 32] for b in self._token_bytes(PAIR_CODE_LENGTH))
            if code not in taken:
                return code

    def pair_confirm(self, code) -> None:
        """The operator typed the code the tablet shows."""
        if not isinstance(code, str):
            raise DeskError(400, Refusal.BAD_CODE)
        wanted = code.upper().replace("-", "").replace(" ", "").translate(_CODE_FIXES)
        if not wanted.isascii():
            raise DeskError(404, Refusal.NO_SUCH_PAIRING)
        with self._mutex:
            self._prune_pairings(self._now())
            for pairing in self._pairings.values():
                if hmac.compare_digest(pairing.code, wanted):
                    pairing.confirmed = True
                    return
            raise DeskError(404, Refusal.NO_SUCH_PAIRING)

    def pair_claim(self, pair_id, secret) -> dict:
        if not isinstance(pair_id, str) or not isinstance(secret, str) or not _HEX64.fullmatch(secret):
            raise DeskError(400, Refusal.BAD_PAIR_CLAIM)
        with self._mutex:
            now = self._now()
            self._prune_pairings(now)
            pairing = self._pairings.get(pair_id)
            if pairing is None:
                raise DeskError(404, Refusal.NO_SUCH_PAIRING)
            if not hmac.compare_digest(sha256_hex(secret), pairing.secret_hash):
                pairing.failures += 1
                if pairing.failures >= MAX_PAIR_SECRET_FAILURES:
                    del self._pairings[pair_id]
                raise DeskError(403, Refusal.WRONG_SECRET)
            if not pairing.confirmed:
                raise DeskError(409, Refusal.NOT_CONFIRMED)
            # No second page may hold a token while a conversation runs.
            self._refuse_claim_during_grant()
            del self._pairings[pair_id]
            return self._issue_token(now)

    def _issue_token(self, now: float) -> dict:
        self._prune_tokens(now)
        while len(self._tokens) >= MAX_TOKENS:
            oldest = min(self._tokens.values(), key=lambda t: t.created)
            del self._tokens[oldest.token_hash]
        token = _b64url(self._token_bytes(TOKEN_BYTES))
        token_hash = sha256_hex(token)
        record = Token(token_hash[:TOKEN_ID_LENGTH], token_hash, now, now + TOKEN_TTL_SECONDS)
        self._tokens[token_hash] = record
        # Long-polls of a dropped token must see it gone.
        self._changed.notify_all()
        return {"token": token, "expires_at": record.expires_at}

    @staticmethod
    def token_hash(token) -> str | None:
        if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
            return None
        return sha256_hex(token)

    def authenticate(self, token) -> bool:
        token_hash = self.token_hash(token)
        with self._mutex:
            return self._token_valid(token_hash)

    @contextlib.contextmanager
    def authorized(self, token):
        """Hold the state's mutex around a mutation and refuse it (401) unless
        `token` is valid now: a revoke can't race a request in flight."""
        token_hash = self.token_hash(token)
        with self._mutex:
            if not self._token_valid(token_hash):
                raise DeskError(401, Refusal.UNAUTHORIZED)
            yield

    @contextlib.contextmanager
    def token_in_use(self, token):
        """Refuse (401) unless `token` is valid now; then, without holding the
        mutex, keep a revoke of it waiting until the block ends."""
        token_hash = self.token_hash(token)
        with self._mutex:
            if not self._token_valid(token_hash):
                raise DeskError(401, Refusal.UNAUTHORIZED)
            self._tokens_in_use[token_hash] = self._tokens_in_use.get(token_hash, 0) + 1
        try:
            yield
        finally:
            with self._mutex:
                left = self._tokens_in_use.pop(token_hash) - 1
                if left:
                    self._tokens_in_use[token_hash] = left
                self._changed.notify_all()

    def revoke(self, token_id: str | None = None) -> Revoked:
        """Delete the matching tokens (all when `token_id` is None), then wait,
        at most `revoke_wait_seconds`, for every acknowledgement still running
        on a matching token that is no longer valid: one this call deleted or
        an earlier revoke did. `settled` is False when the wait ran out, so an
        Enter may still follow."""
        with self._mutex:
            doomed = [k for k, t in self._tokens.items() if token_id is None or t.token_id == token_id]
            for key in doomed:
                del self._tokens[key]
            self._changed.notify_all()

            def running() -> bool:
                return any((token_id is None or key[:TOKEN_ID_LENGTH] == token_id) and key not in self._tokens
                           for key in self._tokens_in_use)
            settled = self._changed.wait_for(lambda: not running(), self.revoke_wait_seconds)
            return Revoked(len(doomed), settled)

    def wait_for_change(self, after, token, timeout: float) -> bool:
        """Sleep until the version differs from `after`, the token stops being
        valid, the hub closes, or `timeout` passes. Returns whether the token
        is still valid."""
        token_hash = self.token_hash(token)
        with self._changed:
            self._changed.wait_for(lambda: self._closed or self._version != after
                                   or not self._token_valid(token_hash), timeout)
            return self._token_valid(token_hash)

    def expire(self) -> None:
        """Housekeeping from the hub's recheck loop."""
        with self._mutex:
            now = self._now()
            self._prune_tickets(now)
            self._prune_pairings(now)
            if self._prune_history(now):
                self._bump()
            if self._prune_tokens(now):
                self._changed.notify_all()
            locked_requests = {lock.request_id for lock in self._locks.values()}
            self._writer_done &= locked_requests

    def close(self) -> None:
        with self._mutex:
            self._closed = True
            self._changed.notify_all()

    # -- views ---------------------------------------------------------------

    def _target_ids(self) -> set[str]:
        return set(self._enrolled) | {r.session_id for r in self._registrations.values()} \
            | {lock.session_id for lock in self._locks.values()}

    def snapshot(self) -> dict:
        """The state API body."""
        with self._mutex:
            targets = [self._target(session_id) for session_id in sorted(self._target_ids())]
            requests = [self._request_view(r) for r in reversed(self._requests.values())]
            return {"version": self._version, "epoch": self.epoch, "sends_enabled": self._sends_enabled,
                    "voice_confirm_enabled": self._voice_enabled,
                    "lock_store_error": self._store_error(), "targets": targets, "requests": requests}

    def _target(self, session_id: str) -> dict:
        found = self._registrations_for(session_id)
        if not found:
            registration, generation = RegistrationState.NONE, None
        elif len(found) > 1:
            registration, generation = RegistrationState.AMBIGUOUS, None
        else:
            registration = RegistrationState.REPLIED if found[0].replied else RegistrationState.REGISTERED
            generation = found[0].generation
        locks = self._locks_for(session_id)
        lock = None
        if locks:
            lock = {"lock_id": locks[0].lock_id, "request_id": locks[0].request_id,
                    "since": locks[0].since, "earlier_run": self._earlier_run(locks[0])}
        label, cwd = self._labels.get(session_id) or (session_id[:8], "")
        return {"host": HOST, "session_id": session_id, "label": label, "cwd": cwd,
                "enrolled": session_id in self._enrolled,
                "registration": registration.value, "generation": generation, "lock": lock}

    def _request_view(self, request: Request) -> dict:
        reply = self._replies.get(request.request_id)
        return {"request_id": request.request_id, "session_id": request.session_id,
                "state": request.state.value, "text": request.text, "created": request.created,
                "confirm": request.confirm.value,
                "reply": {"text": reply.text, "spoken": reply.spoken} if reply else None}

    def status(self) -> dict:
        """What `desk status` prints: ids, states and times, never text."""
        with self._mutex:
            now = self._now()
            self._prune_pairings(now)
            return {
                "epoch": self.epoch,
                "sends_enabled": self._sends_enabled,
                "lock_store_error": self._store_error(),
                "registrations": [
                    {"session_id": r.session_id, "generation": r.generation,
                     "state": self._target(r.session_id)["registration"]}
                    for r in sorted(self._registrations.values(), key=lambda r: r.generation)],
                "locks": [{"lock_id": lock.lock_id, "session_id": lock.session_id,
                           "request_id": lock.request_id, "since": lock.since,
                           "earlier_run": self._earlier_run(lock)}
                          for lock in sorted(self._locks.values(), key=lambda lock: lock.since)],
                "tokens": [{"token_id": t.token_id, "expires_at": t.expires_at}
                           for t in sorted(self._tokens.values(), key=lambda t: t.created)
                           if now < t.expires_at],
                "pending_pairings": len(self._pairings),
                **self._conversation_status(),
            }
