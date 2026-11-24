"""Enrollment edits on the hub (a `Hub` mixin): the Pi's `enroll` and
`unenroll` and the host-qualified `enroll_host` and `unenroll_host`, from
the page and the operator socket, and the operator-only `clear_stopped`
(docs/voice-desk.md, "Enrollment for Mac sessions", "Hub side").

Every edit runs under the policy files' locks and the policy lock (hub.py's
order); a send's handoff rereads the files under them too, so an unenroll
that has returned is never overtaken by a handoff that read the files
before it. An enroll checks both files and the hub's registrations and
locks first (`remote_enrollment.enroll_checked`). An unenroll republishes
and drops, before it returns, every registration of the session that the
new view doesn't route to, so routing stops at once; for a Mac
registration that enqueues `drop` on its link. It recalls nothing already
handed off. Enrolling a running Mac session takes effect at once (no
relaunch: it was launched with the desk flags), and its candidate entry
goes. A phase 1 message carrying `host` is refused rather than read as a
Pi edit. The mixin uses Hub's `_policy_files`, `_policy_lock`, `state`,
`enrollment`, `host_enrollment`, `links`, `refresh_files`, `remote_owner`,
`_drop_connection` and `_now`.
"""

import logging

from .model import HOST, DeskError
from .refusals import Refusal
from .remote_enrollment import check_mac_host, enroll_checked, unenroll_edit

log = logging.getLogger("llm_switchboard.desk")


class HubEnrollment:
    # -- the page (each takes the parsed body and the bearer token) ------------------

    def enroll_host(self, body: dict, token: str) -> dict:
        session_id, host = body.get("session_id"), check_mac_host(body.get("host"))
        with self._policy_files(), self._policy_lock, self.state.authorized(token):
            self._enroll(session_id, host)
        return {"session_id": session_id, "host": host, "enrolled": True}

    def unenroll_host(self, body: dict, token: str) -> dict:
        session_id, host = body.get("session_id"), check_mac_host(body.get("host"))
        with self._policy_files(), self._policy_lock, self.state.authorized(token):
            self._unenroll(session_id, host)
        self._drop_unenrolled(session_id)
        return {"session_id": session_id, "host": host, "enrolled": False}

    # -- the operator socket ------------------------------------------------------------

    def _op_enroll_host(self, message: dict) -> dict:
        session_id, host = message.get("session_id"), check_mac_host(message.get("host"))
        added = self._enroll(session_id, host)
        return {"session_id": session_id, "host": host, "enrolled": True, "changed": added}

    def _op_unenroll_host(self, message: dict) -> dict:
        session_id, host = message.get("session_id"), check_mac_host(message.get("host"))
        removed = self._unenroll(session_id, host)
        self._drop_unenrolled(session_id)
        return {"session_id": session_id, "host": host, "enrolled": False, "changed": removed}

    def _op_clear_stopped(self, message: dict) -> dict:
        """Never offered on the page; no HTTP route reaches it."""
        lock = self.state.clear_stopped(message.get("session_id"), message.get("lock_id"), message.get("confirm"),
                                        lambda found: self.links.link_for_lock(found) is not None)
        log.info("desk: clear-stopped: lock %s of session %s on %s", lock.lock_id, lock.session_id[:8], lock.host)
        return {"cleared": True}

    # -- the edits ---------------------------------------------------------------------

    @staticmethod
    def _refuse_host_key(message: dict) -> None:
        """The phase 1 `enroll` and `unenroll` edit the Pi's file only."""
        if "host" in message:
            raise DeskError(400, Refusal.USE_ENROLL_HOST)

    def _enroll(self, session_id, host: str = HOST) -> bool:
        with self._policy_files(), self._policy_lock:
            added = enroll_checked(self.enrollment, self.host_enrollment, host, session_id, self._now(),
                                   lambda: self.state.held_by_other_host(session_id, host))
            self.refresh_files()
        if host != HOST:
            self.links.candidates.enrolled(host, session_id)
            self.links.publish_candidates()
            log.info("desk: session %s enrolled on %s", session_id[:8], host)
        return added

    def _unenroll(self, session_id, host: str = HOST) -> bool:
        with self._policy_files(), self._policy_lock:
            removed = unenroll_edit(self.enrollment, self.host_enrollment, host, session_id)
            self.refresh_files()
        if host != HOST:
            self.links.candidates.unenrolled(host, session_id)
            self.links.publish_candidates()
        return removed

    def _drop_unenrolled(self, session_id) -> None:
        """Routing stops at once, not at the next recheck: every registration
        of the session from a host the view no longer routes it to."""
        owner = self.remote_owner(session_id)
        for registration in self.state.registrations():
            if registration.session_id == session_id and registration.host != owner:
                self._drop_connection(registration.conn_id, "unenrolled")
