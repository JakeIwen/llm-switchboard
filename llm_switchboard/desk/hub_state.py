"""The hub's in-memory state machine (docs/voice-desk.md, "Hub state").

No sockets, files, clock or randomness of its own: `now()` and
`token_bytes(n)` are injected, and the lock store is a collaborator with
`load()`, `add(lock)` (fsynced before it returns, raises `LockStoreError`)
and `remove(lock_id)`. One mutex guards everything; a condition on it plus
the `version` counter lets long-polls sleep until something visible changes.

A send runs in three steps so the hub can recheck the channel's identity
(I/O, outside this module) between them: `begin_send` (pending) ->
`hand_off` (the lock is persisted and the state recorded, then the caller
writes the event) or `expire_request` when the recheck failed.
"""

import base64
from collections import OrderedDict, deque
import hashlib
import hmac
import re
import threading
from typing import Callable

from .model import (
    CROCKFORD_ALPHABET, EPOCH_BYTES, HOST, LOCK_ID_BYTES, MAX_LOCKS, MAX_PAIR_SECRET_FAILURES,
    MAX_PAIR_STARTS_PER_MINUTE, MAX_PENDING_PAIRINGS, MAX_REGISTRATIONS, MAX_REPLIES,
    MAX_REPLY_RING_BYTES, MAX_REQUESTS, MAX_TICKETS, MAX_TOKENS, OUTSTANDING_STATES, PAIR_CODE_LENGTH,
    PAIR_ID_BYTES, PAIR_TTL_SECONDS, REQUEST_ID_RANDOM_BYTES, TICKET_RANDOM_BYTES, TICKET_TTL_SECONDS,
    TOKEN_BYTES, TOKEN_ID_LENGTH, TOKEN_TTL_SECONDS, DeskError, Lock, LockStoreError, Pairing,
    Registration, RegistrationState, Reply, Request, RequestState, Ticket, Token,
)
from .protocol import MAX_REPLY_TEXT_BYTES, MAX_SPOKEN_BYTES, event_content
from .text_rule import check_text
from ..session_id import is_session_id

_HEX64 = re.compile(r"[0-9a-f]{64}")
_MAX_TOKEN_LENGTH = 128
_PAIR_RATE_WINDOW_SECONDS = 60
_CODE_FIXES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class HubState:
    def __init__(self, lock_store, *, now: Callable[[], float], token_bytes: Callable[[int], bytes],
                 epoch: str | None = None):
        self._mutex = threading.Lock()
        self._changed = threading.Condition(self._mutex)
        self._now = now
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
            raise DeskError(409, "not registered")
        if len(found) > 1:
            raise DeskError(409, "ambiguous")
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
        return "lock file full" if len(self._locks) >= MAX_LOCKS else None

    def _earlier_run(self, lock: Lock) -> bool:
        return not lock.request_id.startswith(self.epoch + ".")

    def _token_valid(self, token_hash: str | None) -> bool:
        token = self._tokens.get(token_hash or "")
        return token is not None and self._now() < token.expires_at

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
                raise DeskError(429, "too many registrations")
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
            lost = []
            for request in self._requests.values():
                if request.conn_id != conn_id:
                    continue
                if request.state is RequestState.HANDED_OFF:
                    request.state = RequestState.UNKNOWN
                    lost.append(request.request_id)
                elif request.state is RequestState.PENDING:
                    request.state = RequestState.EXPIRED
            self._bump()
            return lost

    def registrations(self) -> list[Registration]:
        with self._mutex:
            return [Registration(r.conn_id, r.session_id, r.generation, r.identity, r.verified)
                    for r in self._registrations.values()]

    # -- tickets and sends ---------------------------------------------------

    def issue_ticket(self, session_id, generation, epoch) -> dict:
        if not isinstance(session_id, str) or not is_session_id(session_id):
            raise DeskError(400, "bad session_id")
        if not _is_int(generation) or not isinstance(epoch, str):
            raise DeskError(400, "bad generation or epoch")
        with self._mutex:
            if epoch != self.epoch:
                raise DeskError(409, "stale epoch")
            if self._store_error():
                raise DeskError(503, self._store_error())
            registration = self._routable(session_id)
            if registration.generation != generation:
                raise DeskError(409, "stale generation")
            if self._busy(session_id):
                raise DeskError(409, "busy")
            now = self._now()
            self._prune_tickets(now)
            if len(self._tickets) >= MAX_TICKETS:
                raise DeskError(429, "ticket table full")
            ticket = f"{self.epoch}.{_b64url(self._token_bytes(TICKET_RANDOM_BYTES))}"
            record = Ticket(sha256_hex(ticket), session_id, generation, now + TICKET_TTL_SECONDS)
            self._tickets[record.ticket_hash] = record
            return {"ticket": ticket, "expires_at": record.expires_at}

    def begin_send(self, ticket, text) -> Request:
        """Validate and consume the ticket; the request starts pending."""
        with self._mutex:
            if not self._sends_enabled:
                raise DeskError(403, "sends off")
            if self._store_error():
                raise DeskError(503, self._store_error())
            reason = check_text(text)
            if reason:
                raise DeskError(400, reason)
            record = self._ticket_for_send(ticket)
            registration = self._routable(record.session_id)
            if registration.generation != record.generation:
                raise DeskError(409, "stale generation")
            if self._busy(record.session_id):
                raise DeskError(409, "busy")
            self._make_room_for_request()
            record.consumed = True
            request = Request(
                request_id=f"{self.epoch}.{self._token_bytes(REQUEST_ID_RANDOM_BYTES).hex()}",
                session_id=record.session_id, generation=record.generation,
                conn_id=registration.conn_id, text=text, created=self._now(),
                ticket_hash=record.ticket_hash)
            self._requests[request.request_id] = request
            self._bump()
            return Request(**vars(request))

    def _ticket_for_send(self, ticket) -> Ticket:
        if not isinstance(ticket, str) or len(ticket) > 256:
            raise DeskError(400, "bad ticket")
        epoch, dot, _ = ticket.partition(".")
        if not dot:
            raise DeskError(400, "bad ticket")
        if epoch != self.epoch:
            raise DeskError(409, "stale epoch")
        record = self._tickets.get(sha256_hex(ticket))
        if record is None:
            raise DeskError(409, "unknown ticket")
        if self._now() >= record.expires_at:
            raise DeskError(409, "ticket expired")
        if record.consumed:
            raise DeskError(409, "ticket already used")
        return record

    def _make_room_for_request(self) -> None:
        if len(self._requests) < MAX_REQUESTS:
            return
        for request_id, request in self._requests.items():
            if request.state not in OUTSTANDING_STATES:
                del self._requests[request_id]
                self._replies.pop(request_id, None)
                return
        raise DeskError(429, "too many outstanding requests")

    def request_state(self, request_id: str) -> str | None:
        with self._mutex:
            request = self._requests.get(request_id)
            return request.state.value if request else None

    def expire_request(self, request_id: str) -> None:
        """The dispatch recheck failed before handoff: nothing was written."""
        with self._mutex:
            request = self._requests.get(request_id)
            if request and request.state is RequestState.PENDING:
                request.state = RequestState.EXPIRED
                self._bump()

    def hand_off(self, request_id: str) -> tuple[int, str]:
        """Persist the target lock, record handed_off, and return the
        connection and event content the caller must now write."""
        with self._mutex:
            request = self._requests.get(request_id)
            if request is None or request.state is not RequestState.PENDING:
                raise DeskError(409, "request not pending")
            current = self._registrations.get(request.conn_id)
            others = self._registrations_for(request.session_id)
            if current is None or current.generation != request.generation or len(others) != 1:
                request.state = RequestState.EXPIRED
                self._bump()
                raise DeskError(409, "target changed; not sent")
            lock = Lock(lock_id=self._token_bytes(LOCK_ID_BYTES).hex(), host=HOST,
                        session_id=request.session_id, request_id=request.request_id,
                        ticket_hash=request.ticket_hash, since=self._now())
            try:
                self._lock_store.add(lock)
            except LockStoreError as exc:
                request.state = RequestState.EXPIRED
                reason = str(exc.args[0] if exc.args else "lock store error")
                if reason != "lock file full":
                    self._lock_error = reason
                self._bump()
                raise DeskError(503, reason) from exc
            self._locks[lock.lock_id] = lock
            request.lock_id = lock.lock_id
            request.state = RequestState.HANDED_OFF
            self._bump()
            return request.conn_id, event_content(request.text, request.request_id)

    # -- replies -------------------------------------------------------------

    def handle_reply(self, conn_id: int, request_id, text, spoken) -> str | None:
        """None when this reply is accepted, else why it was dropped."""
        if not all(isinstance(value, str) for value in (request_id, text, spoken)):
            return "bad reply"
        if len(text.encode("utf-8", "surrogatepass")) > MAX_REPLY_TEXT_BYTES or \
                len(spoken.encode("utf-8", "surrogatepass")) > MAX_SPOKEN_BYTES:
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
            request.state = RequestState.ANSWERED
            self._registrations[conn_id].verified = True
            self._remember_reply(Reply(request_id, text, spoken))
            self._bump()
            return None

    def _remember_reply(self, reply: Reply) -> None:
        self._replies[reply.request_id] = reply
        while len(self._replies) > MAX_REPLIES or \
                sum(r.size for r in self._replies.values()) > MAX_REPLY_RING_BYTES:
            self._replies.popitem(last=False)

    # -- locks ----------------------------------------------------------------

    def clear_lock(self, session_id, lock_id) -> None:
        """Jacob checked the session: release exactly the lock he saw."""
        if not isinstance(session_id, str) or not isinstance(lock_id, str):
            raise DeskError(400, "bad session_id or lock_id")
        with self._mutex:
            locks = self._locks_for(session_id)
            if not locks:
                raise DeskError(409, "no lock")
            lock = next((lock for lock in locks if lock.lock_id == lock_id), None)
            if lock is None:
                raise DeskError(409, "lock id does not match")
            try:
                self._lock_store.remove(lock.lock_id)
            except LockStoreError as exc:
                self._lock_error = str(exc.args[0] if exc.args else "lock store error")
                self._bump()
                raise DeskError(503, self._lock_error) from exc
            del self._locks[lock.lock_id]
            request = self._requests.get(lock.request_id)
            # Nobody waits for it any more, so a late reply is dropped.
            if request and request.state is RequestState.HANDED_OFF:
                request.state = RequestState.UNKNOWN
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

    # -- pairing and tokens --------------------------------------------------

    def pair_start(self, secret_hash) -> dict:
        if not isinstance(secret_hash, str) or not _HEX64.fullmatch(secret_hash):
            raise DeskError(400, "bad secret_hash")
        with self._mutex:
            now = self._now()
            while self._pair_starts and now - self._pair_starts[0] >= _PAIR_RATE_WINDOW_SECONDS:
                self._pair_starts.popleft()
            if len(self._pair_starts) >= MAX_PAIR_STARTS_PER_MINUTE:
                raise DeskError(429, "too many pairing attempts")
            self._pair_starts.append(now)
            self._prune_pairings(now)
            if len(self._pairings) >= MAX_PENDING_PAIRINGS:
                raise DeskError(429, "too many pending pairings")
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
            raise DeskError(400, "bad code")
        wanted = code.upper().replace("-", "").replace(" ", "").translate(_CODE_FIXES)
        if not wanted.isascii():
            raise DeskError(404, "no such pairing")
        with self._mutex:
            self._prune_pairings(self._now())
            for pairing in self._pairings.values():
                if hmac.compare_digest(pairing.code, wanted):
                    pairing.confirmed = True
                    return
            raise DeskError(404, "no such pairing")

    def pair_claim(self, pair_id, secret) -> dict:
        if not isinstance(pair_id, str) or not isinstance(secret, str) or not _HEX64.fullmatch(secret):
            raise DeskError(400, "bad pair_id or secret")
        with self._mutex:
            now = self._now()
            self._prune_pairings(now)
            pairing = self._pairings.get(pair_id)
            if pairing is None:
                raise DeskError(404, "no such pairing")
            if not hmac.compare_digest(sha256_hex(secret), pairing.secret_hash):
                pairing.failures += 1
                if pairing.failures >= MAX_PAIR_SECRET_FAILURES:
                    del self._pairings[pair_id]
                raise DeskError(403, "wrong secret")
            if not pairing.confirmed:
                raise DeskError(409, "not confirmed")
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

    def revoke(self, token_id: str | None = None) -> int:
        with self._mutex:
            doomed = [k for k, t in self._tokens.items() if token_id is None or t.token_id == token_id]
            for key in doomed:
                del self._tokens[key]
            self._changed.notify_all()
            return len(doomed)

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
            if self._prune_tokens(now):
                self._changed.notify_all()

    def close(self) -> None:
        with self._mutex:
            self._closed = True
            self._changed.notify_all()

    # -- views ---------------------------------------------------------------

    def snapshot(self) -> dict:
        """The state API body, without each target's label and cwd (the hub
        adds those from disk outside the lock)."""
        with self._mutex:
            session_ids = set(self._enrolled) | {r.session_id for r in self._registrations.values()} \
                | {lock.session_id for lock in self._locks.values()}
            targets = [self._target(session_id) for session_id in sorted(session_ids)]
            requests = [self._request_view(r) for r in reversed(self._requests.values())]
            return {"version": self._version, "epoch": self.epoch, "sends_enabled": self._sends_enabled,
                    "lock_store_error": self._store_error(), "targets": targets, "requests": requests}

    def _target(self, session_id: str) -> dict:
        found = self._registrations_for(session_id)
        if not found:
            registration, generation = RegistrationState.NONE, None
        elif len(found) > 1:
            registration, generation = RegistrationState.AMBIGUOUS, None
        else:
            registration = RegistrationState.VERIFIED if found[0].verified else RegistrationState.REGISTERED
            generation = found[0].generation
        locks = self._locks_for(session_id)
        lock = None
        if locks:
            lock = {"lock_id": locks[0].lock_id, "request_id": locks[0].request_id,
                    "since": locks[0].since, "earlier_run": self._earlier_run(locks[0])}
        return {"host": HOST, "session_id": session_id, "enrolled": session_id in self._enrolled,
                "registration": registration.value, "generation": generation, "lock": lock}

    def _request_view(self, request: Request) -> dict:
        reply = self._replies.get(request.request_id)
        return {"request_id": request.request_id, "session_id": request.session_id,
                "state": request.state.value, "text": request.text, "created": request.created,
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
            }
