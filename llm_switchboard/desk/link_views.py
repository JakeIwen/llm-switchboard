"""The Mac link's part of the hub state (a `HubState` mixin, like
`grants.ConversationGrants`): the link records and the unattributed
admission refusals `link_hub.LinkHub` publishes (`set_links`,
`set_admission_refusals`), their snapshot and `status` fields, and the
lock lookup a clear makes before it asks a Mac for presence
(`lock_info`), and a Mac target's label as its link registered it
(`set_label`). The mixin uses HubState's `_mutex`, `_bump`, `_locks`,
`_labels`, `_in_delivery` and `_writer_done`. Standard library only.
"""

from enum import StrEnum

from .model import HOST, Lock


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
