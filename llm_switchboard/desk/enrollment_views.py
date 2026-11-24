"""Enrollment in the hub state (a `HubState` mixin, like `link_views` and
`grants`): the published view `{session id: host}` with its `conflicts`,
which `Hub.refresh_files` sets whole from both enrollment files in one
`set_enrollment` call so no reader sees one file's change without the
other's; the routing checks over it (an id with two owners routes nowhere,
and a registration routes only when the view names its host); the hub's
part of an enroll's check (registrations and locks on another host); and
the candidates `link_hub` publishes (`set_candidates`). The mixin uses
HubState's `_mutex`, `_bump`, `_registrations`, `_locks` and `_lock_error`.
Standard library only.
"""

from .model import DeskError, Registration
from .refusals import Refusal


class EnrollmentViews:
    # Replaced whole, never mutated, so the class defaults are safe.
    _enrollment: dict = {}
    _conflicts: frozenset = frozenset()
    _candidates: tuple = ()

    def set_enrollment(self, enrollment: dict, conflicts=frozenset()) -> None:
        """The published view: {session id: host} and the ids enrolled on two hosts."""
        enrollment, conflicts = dict(enrollment), frozenset(conflicts)
        with self._mutex:
            if (enrollment, conflicts) != (self._enrollment, self._conflicts):
                self._enrollment, self._conflicts = enrollment, conflicts
                self._bump()

    def set_candidates(self, candidates) -> None:
        """`[{host, session_id, label, folder, seen}]` (link_candidates.py)."""
        candidates = tuple(dict(candidate) for candidate in candidates)
        with self._mutex:
            if candidates != self._candidates:
                self._candidates = candidates
                self._bump()

    def held_by_other_host(self, session_id: str, host: str) -> bool:
        """An enroll's check against the hub: a live registration or a lock
        of `session_id` on another host. A lock store that failed can't be
        checked, so the edit is refused (503)."""
        with self._mutex:
            if self._lock_error:
                raise DeskError(503, Refusal.LOCKS_UNREADABLE)
            return any(r.session_id == session_id and r.host != host for r in self._registrations.values()) \
                or any(lock.session_id == session_id and lock.host != host for lock in self._locks.values())

    # -- routing (caller holds the mutex) ----------------------------------------------

    def _check_enrolled(self, session_id: str) -> None:
        if session_id in self._conflicts:
            raise DeskError(409, Refusal.ENROLLED_ON_TWO_HOSTS)
        if session_id not in self._enrollment:
            raise DeskError(409, Refusal.NOT_ENROLLED)

    def _check_host(self, registration: Registration) -> None:
        """A registration routes only from the host the view names for it."""
        if self._enrollment.get(registration.session_id) != registration.host:
            raise DeskError(409, Refusal.ENROLLED_ON_ANOTHER_HOST)

    # -- views (caller holds the mutex) ------------------------------------------------

    def _enrollment_ids(self) -> set[str]:
        return set(self._enrollment) | set(self._conflicts)

    def _candidate_fields(self, *, folders: bool) -> dict:
        """The page gets each candidate whole; `desk status` without the folder."""
        return {"candidates": [dict(c) if folders else {k: v for k, v in c.items() if k != "folder"}
                               for c in self._candidates]}
