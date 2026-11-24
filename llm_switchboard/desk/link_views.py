"""The Mac link's part of the hub state (a `HubState` mixin, like
`grants.ConversationGrants`): the link records and the unattributed
admission refusals `link_hub.LinkHub` publishes (`set_links`,
`set_admission_refusals`), their snapshot and `status` fields, and the
lock lookup a clear makes before it asks a Mac for presence
(`lock_info`), a Mac target's label as its link registered it
(`set_label`), a Mac lock's row in `desk status`, and the operator-only
`clear_stopped`. The mixin uses HubState's `_mutex`, `_bump`, `_locks`,
`_labels`, `_in_delivery`, `_writer_done`, `_lock_store`, `_lock_error`,
`_requests`, `_finish` and `_earlier_run`. Standard library only.
"""

from enum import StrEnum
from typing import Callable

from .model import HOST, MACHINE_PREFIX_LENGTH, DeskError, Lock, LockStoreError, RequestState
from .refusals import Refusal


class LinkState(StrEnum):
    """A host's link as the desk shows it."""
    UP = "up"
    DOWN = "down"
    # The verifier failed its self-test at hub start: no link is admitted.
    VERIFIER_UNAVAILABLE = "verifier_unavailable"


class LinkViews:
    _link_records: tuple = ()
    _admission_refusals: tuple = ()

    def set_links(self, records) -> None:
        """`[{host, state, since, signer}]` for every host the link knows."""
        records = tuple(dict(record) for record in records)
        with self._mutex:
            if records != self._link_records:
                self._link_records = records
                self._bump()

    def set_admission_refusals(self, refusals) -> None:
        """`[{code, count, last, versions}]`, one entry per `link_refused` code."""
        refusals = tuple(dict(refusal) for refusal in refusals)
        with self._mutex:
            if refusals != self._admission_refusals:
                self._admission_refusals = refusals
                self._bump()

    def set_label(self, session_id: str, label: str, cwd: str) -> None:
        """One Mac target's label and folder, as its link registered it."""
        with self._mutex:
            if self._labels.get(session_id) != (label, cwd):
                self._labels[session_id] = (label, cwd)
                self._bump()

    def lock_info(self, session_id, lock_id) -> tuple[Lock, bool] | None:
        """The lock a clear names and whether clearing it needs a presence
        answer (no delivery report or proof of no write in this run, and
        no delivery in progress), read under the state's lock."""
        if not isinstance(session_id, str) or not isinstance(lock_id, str):
            return None
        with self._mutex:
            lock = self._locks.get(lock_id)
            if lock is None or lock.session_id != session_id:
                return None
            needs = lock.request_id not in self._writer_done and lock.request_id not in self._in_delivery
            return lock, needs

    def clear_stopped(self, session_id, lock_id, confirm, link_matches: Callable[[Lock], bool]) -> Lock:
        """Jacob saw this Mac session exit and its Mac can't answer for it
        (the link removed for good, the key rotated, the Mac replaced):
        release the lock without presence. Everything is checked here,
        under the state's lock, at the mutation: `confirm` repeats the lock
        id, the lock exists with that id and session, it is not the Pi's,
        no current link for its host has its key and machine
        (`link_matches`, which takes only the links lock; then the presence
        clear is the way), and its request is not in delivery."""
        if not isinstance(session_id, str) or not isinstance(lock_id, str):
            raise DeskError(400, Refusal.BAD_LOCK_REF)
        if confirm != lock_id:
            raise DeskError(400, Refusal.CLEAR_STOPPED_NEEDS_CONFIRM)
        with self._mutex:
            if not any(lock.session_id == session_id for lock in self._locks.values()):
                raise DeskError(409, Refusal.NO_LOCK)
            lock = self._locks.get(lock_id)
            if lock is None or lock.session_id != session_id:
                raise DeskError(409, Refusal.LOCK_ID_MISMATCH)
            if lock.host == HOST:
                raise DeskError(409, Refusal.CLEAR_STOPPED_PI_LOCK)
            if link_matches(lock):
                raise DeskError(409, Refusal.CLEAR_STOPPED_LINK_UP)
            if lock.request_id in self._in_delivery:
                raise DeskError(409, Refusal.DELIVERY_IN_PROGRESS)
            try:
                self._lock_store.remove(lock.lock_id)
            except LockStoreError as exc:
                self._lock_error = str(exc.args[0] if exc.args else "lock store error")
                self._bump()
                raise DeskError(503, self._lock_error) from exc
            del self._locks[lock.lock_id]
            self._writer_done.discard(lock.request_id)
            request = self._requests.get(lock.request_id)
            if request and request.state is RequestState.HANDED_OFF:
                self._finish(request, RequestState.UNKNOWN)
            self._bump()
            return lock

    def _lock_status(self, lock: Lock) -> dict:
        """A lock as `desk status` lists it; a Mac lock adds its key and the
        start of its machine id, which `desk clear-stopped` shows."""
        row = {"lock_id": lock.lock_id, "host": lock.host, "session_id": lock.session_id,
               "request_id": lock.request_id, "since": lock.since, "earlier_run": self._earlier_run(lock)}
        if lock.host != HOST:
            row.update(link_key=lock.link_key, machine=(lock.machine or "")[:MACHINE_PREFIX_LENGTH])
        return row

    def _links_view(self) -> list[dict]:
        """The published records, plus a down entry for any other host a
        lock names (caller holds the mutex)."""
        views = {record["host"]: dict(record) for record in self._link_records}
        for lock in self._locks.values():
            if lock.host != HOST and lock.host not in views:
                views[lock.host] = {"host": lock.host, "state": LinkState.DOWN.value, "since": None, "signer": False}
        return [views[host] for host in sorted(views)]

    def _link_fields(self) -> dict:
        return {"links": self._links_view(), "admission_refusals": [dict(r) for r in self._admission_refusals]}
