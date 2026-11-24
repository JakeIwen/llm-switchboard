"""Conversation grants: the hub's side of conversation mode (docs/voice-desk.md,
"Conversation mode").

A grant lets the one paired page send prompts confirmed by a spoken "send it"
(`ConfirmMode.VOICE`) to one frozen target. It is given only to a token that
is the only valid token, only while the voice gate is on, and only for a
target that could take a ticket now. It expires `GRANT_TTL_SECONDS` after the
hub last renewed it, measured on the state's `elapsed()` clock so a wall-clock
correction can neither extend nor cut a lease; an expired grant is never
revived. At most one exists:
a new grant replaces the old one, so a delayed end or beat naming the old id
touches nothing. Beat, end, `begin_send` and `hand_off` all require the exact
current, unexpired grant for that token (and, for a send, that target). A
revoked, expired or evicted token ends its grant, since every check asks
whether the grant's token is still valid. While a grant is live, pairing
claims are refused. Grants live in memory and die with the epoch.

This is a mixin of `HubState`: every method here runs under the state's
mutex and uses its token, routing and registration helpers, so a grant check
and the state it guards can't drift apart. The voice gate it reads
(`_voice_enabled`) is published by the hub from a fresh read of
`desk-settings.json` before every handoff (`Hub._prepare_handoff`).
"""

from dataclasses import dataclass
import hmac
import re

from .model import MAX_VOICE_TEXT_BYTES, ConfirmMode, DeskError, RequestState
from .refusals import Refusal
from ..session_id import is_session_id

GRANT_TTL_SECONDS = 15
GRANT_ID_BYTES = 16
# The shape `grant_conversation` issues; anything else is refused before the
# constant-time comparison, which raises on non-ASCII text.
_GRANT_ID = re.compile(f"[0-9a-f]{{{GRANT_ID_BYTES * 2}}}")

# Their former names, kept for callers.
GRANT_NOT_CURRENT = Refusal.GRANT_NOT_CURRENT
GRANT_WRONG_TARGET = Refusal.GRANT_WRONG_TARGET
ANOTHER_PAGE = Refusal.ANOTHER_PAGE
VOICE_OFF = Refusal.VOICE_OFF
CONVERSATION_LIVE = Refusal.CONVERSATION_LIVE
VOICE_TEXT_TOO_LONG = Refusal.VOICE_TEXT_TOO_LONG
TARGET_CHANGED = Refusal.TARGET_CHANGED
BAD_CONFIRM = Refusal.BAD_CONFIRM


@dataclass
class Grant:
    grant_id: str
    token_hash: str
    session_id: str
    generation: int
    # When the hub last created or renewed it, on the elapsed-time clock; it
    # expires GRANT_TTL_SECONDS later.
    renewed: float


def confirm_mode(value) -> ConfirmMode:
    """A send's `confirm` field as supplied: only "tap" or "voice". Callers
    pass `ConfirmMode.TAP` when the field is omitted (a tap, as before);
    anything supplied, JSON null included, is refused unless it names a mode."""
    if isinstance(value, str):
        try:
            return ConfirmMode(value)
        except ValueError:
            pass
    raise DeskError(400, Refusal.BAD_CONFIRM)


class ConversationGrants:
    """Mixed into HubState; see the module docstring. Callers hold `_mutex`
    in every `_`-prefixed method."""

    _grant: Grant | None = None
    _voice_enabled: bool = False

    def _live_grant(self) -> Grant | None:
        grant = self._grant
        if grant is not None and (self._elapsed() >= grant.renewed + GRANT_TTL_SECONDS
                                  or not self._token_valid(grant.token_hash)):
            self._grant = grant = None
        return grant

    def _current_grant(self, grant_id, token_hash) -> Grant:
        grant = self._live_grant()
        if (grant is None or not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id)
                or grant.token_hash != token_hash or not hmac.compare_digest(grant.grant_id, grant_id)):
            raise DeskError(409, Refusal.GRANT_NOT_CURRENT)
        return grant

    def _authorized_hash(self, token) -> str:
        token_hash = self.token_hash(token)
        if not self._token_valid(token_hash):
            raise DeskError(401, Refusal.UNAUTHORIZED)
        return token_hash

    def grant_conversation(self, token, session_id, generation, epoch) -> dict:
        if not isinstance(session_id, str) or not is_session_id(session_id):
            raise DeskError(400, Refusal.BAD_SESSION_ID)
        if not isinstance(generation, int) or isinstance(generation, bool) or not isinstance(epoch, str):
            raise DeskError(400, Refusal.BAD_GENERATION_OR_EPOCH)
        with self._mutex:
            token_hash = self._authorized_hash(token)
            if epoch != self.epoch:
                raise DeskError(409, Refusal.STALE_EPOCH)
            # Expired tokens are deleted, as housekeeping does, before the
            # count: one merely left out of it would become valid again if
            # the wall clock stepped back, and a second page would hold a
            # token during this grant.
            if self._prune_tokens(self._now()):
                self._changed.notify_all()
            # The prune reads the clock again: the requester's own token may
            # have expired since it was authorized, leaving another as the one.
            if token_hash not in self._tokens:
                raise DeskError(401, Refusal.UNAUTHORIZED)
            if len(self._tokens) != 1:
                raise DeskError(409, Refusal.ANOTHER_PAGE)
            if not self._voice_enabled:
                raise DeskError(403, Refusal.VOICE_OFF)
            self._check_routing(session_id, sending=True)
            if self._routable(session_id).generation != generation:
                raise DeskError(409, Refusal.STALE_GENERATION)
            if self._busy(session_id):
                raise DeskError(409, Refusal.BUSY)
            # Replaces any earlier grant. Grants aren't in the snapshot, so no version bump.
            self._grant = Grant(self._token_bytes(GRANT_ID_BYTES).hex(), token_hash, session_id, generation,
                                self._elapsed())
            return {"grant": self._grant.grant_id, "expires_in": GRANT_TTL_SECONDS}

    def beat_grant(self, token, grant_id) -> dict:
        """Renew the current grant; its target must still be the same single
        registration, or the grant ends."""
        with self._mutex:
            grant = self._current_grant(grant_id, self._authorized_hash(token))
            found = self._registrations_for(grant.session_id)
            if len(found) != 1 or found[0].generation != grant.generation:
                self._grant = None
                raise DeskError(409, Refusal.TARGET_CHANGED)
            grant.renewed = self._elapsed()
            return {"grant": grant.grant_id, "expires_in": GRANT_TTL_SECONDS}

    def end_grant(self, token, grant_id) -> dict:
        with self._mutex:
            self._current_grant(grant_id, self._authorized_hash(token))
            self._grant = None
            return {"ended": True}

    def _admit_send_mode(self, confirm, grant_id, token_hash, ticket, text) -> tuple[ConfirmMode, str | None]:
        """At `begin_send`: a voice send needs the voice gate on, the current
        grant for this token and the ticket's target, and short text."""
        mode = confirm_mode(confirm)
        if mode is ConfirmMode.TAP:
            return mode, None
        if not self._voice_enabled:
            raise DeskError(403, Refusal.VOICE_OFF)
        grant = self._current_grant(grant_id, token_hash)
        if grant.session_id != ticket.session_id or grant.generation != ticket.generation:
            raise DeskError(409, Refusal.GRANT_WRONG_TARGET)
        if len(text.encode()) > MAX_VOICE_TEXT_BYTES:
            raise DeskError(400, Refusal.VOICE_TEXT_TOO_LONG)
        return mode, grant.grant_id

    def _check_voice_handoff(self, request) -> None:
        """At `hand_off`: the gate and the very grant the send was accepted under."""
        if request.confirm is not ConfirmMode.VOICE:
            return
        if not self._voice_enabled:
            raise DeskError(403, Refusal.VOICE_OFF)
        grant = self._current_grant(request.grant_id, request.token_hash)
        if grant.session_id != request.session_id or grant.generation != request.generation:
            raise DeskError(409, Refusal.GRANT_WRONG_TARGET)

    def _recheck_voice_after_lock(self, request, lock) -> None:
        """At `hand_off`, after the durable lock write and before handed_off
        is recorded: that write can be slow (an SD card), and the grant can
        expire while it runs. Then the handoff is refused with in-memory work
        only, since a second store write here would hold the state's lock
        (and the hub's policy and file locks around it) through another slow
        write. The lock just written stays, in memory as on disk, so the
        target stays busy; nothing has reached the channel, so it is marked
        never written and a clear (the page's Clear or `desk clear`) releases
        it with no channel-process check."""
        if request.confirm is not ConfirmMode.VOICE:
            return
        try:
            self._check_voice_handoff(request)
        except DeskError:
            self._locks[lock.lock_id] = lock
            self._writer_done.add(request.request_id)
            self._finish(request, RequestState.EXPIRED)
            self._bump()
            raise

    def _refuse_claim_during_grant(self) -> None:
        if self._live_grant() is not None:
            raise DeskError(409, Refusal.CONVERSATION_LIVE)

    def set_voice_enabled(self, enabled: bool) -> None:
        with self._mutex:
            if self._voice_enabled != bool(enabled):
                self._voice_enabled = bool(enabled)
                self._bump()

    def _conversation_status(self) -> dict:
        """For `desk status`: ids only."""
        grant = self._live_grant()
        token = self._tokens.get(grant.token_hash) if grant else None
        return {"voice_confirm_enabled": self._voice_enabled,
                "conversation": {"session_id": grant.session_id, "generation": grant.generation,
                                 "token_id": token.token_id if token else None} if grant else None}
