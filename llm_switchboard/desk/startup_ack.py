"""The startup acknowledgement: show an enrolled session's development-channel
warning and, when Jacob taps the button (or confirms `desk ack`), press Enter
once in its pane (docs/voice-desk.md, "Startup acknowledgement").

The hub sends the Enter itself, over the dedicated tmux server's socket,
rather than asking the supervisor: the supervisor runs in the picker's
process with no authenticated path from the hub, and keys typed into a
supervisor window from outside its loop already have a precedent (the
picker types `/rename`). Only the supervisor starts or kills windows.

Order of an acknowledgement: every identity, registry, enrollment and
token check first, and room in the attempt table (its /proc reads stay
outside the token hold); then the attempt is recorded (one Enter per
displayed view, even when the outcome is unknown); then the final capture,
the digest comparison and the guarded send, with nothing else between them. The send
is one tmux command whose `if-shell -F` guard re-tests the pane (id, pid,
window, tag, a single pane, not in a mode, input on) and the warning's rows
on the server's own screen in the same pass as the key.

Lock order (docs/voice-desk.md, "Hub state"): an acknowledgement takes its
own `_sending` lock without waiting (another one in progress is refused),
then `enrolled.json`'s lock, first in the hub's order and re-entrant in this
process. It holds that file lock across its tmux calls and /proc reads, so
an unenroll that has returned is never overtaken by an Enter that read the
enrollment before it. It never takes the policy lock, and takes the state's
mutex only briefly inside `authorize()`'s context (the hub's
`token_in_use`), never across a tmux call or a lock wait; a revoke waits
for that context to end instead. Every tmux call times out after
STARTUP_TMUX_TIMEOUT_SECONDS, which bounds how long housekeeping and a
send's handoff wait for that file lock.

What stays non-atomic, best-effort by Jacob's decision (2026-11-22): tmux
can only test its own copy of the screen. A key that Jacob (attached) or
Remote Control typed just before ours, and that Claude hasn't processed
yet, leaves the warning on tmux's screen, so our Enter then lands on the
next screen, normally Claude's empty prompt (where an empty Enter does
nothing). Between the final capture and the guarded send is one tmux round
trip; the guard covers the pane and the warning's rows there, not the
claude process inside the pane, which was checked before the capture.

Logs carry session ids and refusal reasons only, never screen content.
"""

from collections import deque
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Callable, ContextManager

from ..live_registry import managed_for
from ..screen import (
    DESK_WARNING_ACCEPT_OPTION, DESK_WARNING_GUARD_ABSENT, DESK_WARNING_GUARD_PRESENT, desk_startup_selection,
)
from ..session_id import is_session_id
from ..tmux import GuardedSend, Pane, PaneGuard, Tmux, TmuxError
from .config import DeskConfig
from .enrollment import Enrollment
from .model import (
    MAX_STARTUP_ACKS_PER_MINUTE, MAX_STARTUP_ATTEMPTS, MAX_STARTUP_VIEWS_PER_MINUTE, SHA256_HEX_PATTERN,
    ChannelProcess, DeskError,
)
from .peer import ProcessRecord, process_presence, read_parent_pid, read_process_entry
from .startup_identity import (
    LaunchIdentity, StartupRefusal, check_launch_argv, check_session_file, foreground_claude, refused,
    view_digest,
)
from .startup_registry import read_session_entry

log = logging.getLogger("llm_switchboard.desk")

_RATE_WINDOW_SECONDS = 60
_TMUX_ERRORS = (TmuxError, subprocess.SubprocessError, OSError)


class _RateWindow:
    """At most `limit` takes per window, across every caller."""

    def __init__(self, limit: int, now: Callable[[], float]):
        self._limit = limit
        self._now = now
        self._taken: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            now = self._now()
            while self._taken and now - self._taken[0] >= _RATE_WINDOW_SECONDS:
                self._taken.popleft()
            if len(self._taken) >= self._limit:
                return False
            self._taken.append(now)
            return True


@dataclass
class _Attempt:
    identity: LaunchIdentity
    # tmux answered that the guarded Enter was sent; False while it is
    # running or when its outcome is unknown.
    confirmed: bool = False


class _Attempts:
    """Views that got, or may have got, their Enter in this hub run.
    Recorded before the key, so a timeout or an ambiguous send still
    counts. A record is removed only on proof: `forget` when nothing was
    typed for certain, or `make_room` once its claude process is proven gone
    (`process_presence` says GONE). An unreadable process keeps its record,
    and a full table refuses new acknowledgements rather than evicting one."""

    def __init__(self, proc_dir: Path):
        self._proc_dir = proc_dir
        self._seen: dict[str, _Attempt] = {}
        self._lock = threading.Lock()

    def refusal_for(self, digest: str) -> StartupRefusal | None:
        """Why this view gets no Enter, or None when it had no attempt."""
        with self._lock:
            attempt = self._seen.get(digest)
        if attempt is None:
            return None
        return StartupRefusal.ALREADY_SENT if attempt.confirmed else StartupRefusal.ALREADY_ATTEMPTED

    def make_room(self) -> None:
        """Drop the records whose claude is proven gone, then refuse (503)
        when the table is still full. It reads /proc once per record, so it
        runs before the token is held, never inside `token_in_use`."""
        with self._lock:
            seen = list(self._seen.items())
        gone = [digest for digest, attempt in seen
                if process_presence(self._proc_dir, attempt.identity.claude_pid, attempt.identity.claude_start)
                is ChannelProcess.GONE]
        with self._lock:
            for digest in gone:
                self._seen.pop(digest, None)
            if len(self._seen) >= MAX_STARTUP_ATTEMPTS:
                raise refused(StartupRefusal.ATTEMPTS_FULL)

    def record(self, digest: str, identity: LaunchIdentity) -> None:
        with self._lock:
            # `make_room` ran under the same `_sending` lock; this is its backstop.
            if len(self._seen) >= MAX_STARTUP_ATTEMPTS:
                raise refused(StartupRefusal.ATTEMPTS_FULL)
            self._seen[digest] = _Attempt(identity)

    def confirm(self, digest: str) -> None:
        with self._lock:
            if digest in self._seen:
                self._seen[digest].confirmed = True

    def forget(self, digest: str) -> None:
        with self._lock:
            self._seen.pop(digest, None)


@dataclass(frozen=True)
class _Inspection:
    session_id: str
    capture: str | None
    # The warning's selected option, or None when the screen isn't the warning.
    selection: int | None
    identity: LaunchIdentity | None
    refusal: StartupRefusal | None
    digest: str | None = None


class StartupAck:
    def __init__(self, config: DeskConfig, enrollment: Enrollment, tmux: Tmux, epoch: str,
                 now: Callable[[], float] = time.monotonic):
        self.config = config
        self.enrollment = enrollment
        self.tmux = tmux
        self.epoch = epoch
        self._views = _RateWindow(MAX_STARTUP_VIEWS_PER_MINUTE, now)
        self._acks = _RateWindow(MAX_STARTUP_ACKS_PER_MINUTE, now)
        self._sending = threading.Lock()
        self._attempts = _Attempts(config.proc_dir)

    # -- public entry points ---------------------------------------------------

    def blocked(self) -> list[dict]:
        """Every enrolled session whose supervisor pane shows the warning, with
        its capture and, when it may be acknowledged, the digest to send back.
        The screen is read first; only a pane showing the warning gets the
        process checks. A failed read is never reported as absence: a pane
        whose screen tmux couldn't read is listed with TMUX_FAILED, and an
        unreadable `enrolled.json` or pane listing fails the whole call (503);
        only a pane tmux no longer lists is skipped."""
        if not self._views.take():
            raise refused(StartupRefusal.TOO_MANY)
        try:
            panes = self.tmux.panes()
        except _TMUX_ERRORS:
            raise refused(StartupRefusal.TMUX_FAILED) from None
        enrolled = set(self.enrollment.sessions(strict=True))
        candidates = set()
        for pane in panes:
            if pane.session_id not in enrolled:
                continue
            try:
                screen = self._capture_live(pane.pane_id)
            except _TMUX_ERRORS:
                # Unknown screen: inspected below, and listed if it fails again.
                candidates.add(pane.session_id)
                continue
            if screen is not None and desk_startup_selection(screen) is not None:
                candidates.add(pane.session_id)
        found = []
        for session_id in sorted(candidates):
            inspection = self._inspect(session_id)
            if inspection.selection is not None or inspection.refusal is StartupRefusal.TMUX_FAILED:
                found.append(self._item(inspection))
        return found

    def view(self, session_id) -> dict:
        """One session's warning, for `desk ack`; refused when it isn't showing."""
        _check_session_id(session_id)
        if not self._views.take():
            raise refused(StartupRefusal.TOO_MANY)
        inspection = self._inspect(session_id)
        if inspection.selection is None:
            raise refused(inspection.refusal or StartupRefusal.NOT_BLOCKED)
        return self._item(inspection)

    def acknowledge(self, session_id, digest, authorize: Callable[[], ContextManager]) -> dict:
        """One Enter in the session's pane, only if everything the digest was
        issued for still holds. `authorize()` is a context that raises (401)
        when the caller's token stopped being valid and holds off its revoke
        until the Enter is out; the operator socket passes a no-op."""
        _check_session_id(session_id)
        if not isinstance(digest, str) or not SHA256_HEX_PATTERN.fullmatch(digest):
            raise DeskError(400, "bad digest")
        if not self._acks.take():
            raise refused(StartupRefusal.TOO_MANY)
        if not self._sending.acquire(blocking=False):
            raise refused(StartupRefusal.BUSY)
        try:
            with self.enrollment.locked():
                identity = self._precheck(session_id, digest)
                guard = _pane_guard(identity)
                self._attempts.make_room()
                with authorize():
                    self._attempts.record(digest, identity)
                    self._capture_compare_send(identity, digest, guard)
        except DeskError as exc:
            log.info("desk: startup ack for session %s refused: %s", session_id[:8], exc.reason)
            raise
        finally:
            self._sending.release()
        log.info("desk: startup ack for session %s: Enter sent", session_id[:8])
        return {"session_id": session_id, "sent": True}

    # -- the acknowledgement's steps ---------------------------------------------

    def _precheck(self, session_id: str, digest: str) -> LaunchIdentity:
        """Everything but the final screen: the pane, the process, the
        session file and enrollment, read twice around a capture (a change
        during it is a refusal), and the digest compared with that capture."""
        inspection = self._inspect(session_id)
        if inspection.refusal is not None:
            raise refused(inspection.refusal)
        if inspection.digest != digest:
            raise refused(StartupRefusal.DIGEST_MISMATCH)
        earlier = self._attempts.refusal_for(digest)
        if earlier is not None:
            raise refused(earlier)
        return inspection.identity

    def _capture_compare_send(self, identity: LaunchIdentity, digest: str, guard: PaneGuard) -> None:
        """The final capture and the guarded key, with only the digest
        comparison between them. A failure before the key is certain to have
        typed nothing, so its attempt is forgotten; once tmux has the key
        command, an unclear answer keeps the attempt and says so (503)."""
        try:
            capture = self.tmux.capture(identity.pane_id, check=True)
        except _TMUX_ERRORS:
            self._attempts.forget(digest)
            raise refused(StartupRefusal.TMUX_FAILED) from None
        if view_digest(identity, capture, self.epoch) != digest:
            self._attempts.forget(digest)
            raise refused(StartupRefusal.CHANGED)
        try:
            outcome = self.tmux.send_enter_if(identity.pane_id, guard)
        except _TMUX_ERRORS as exc:
            log.warning("desk: startup ack for session %s: tmux did not confirm the Enter (%s)",
                        identity.session_id[:8], type(exc).__name__)
            raise refused(StartupRefusal.DELIVERY_UNKNOWN) from None
        if outcome is not GuardedSend.SENT:
            self._attempts.forget(digest)
            raise refused(StartupRefusal.CHANGED)
        self._attempts.confirm(digest)

    # -- checks ------------------------------------------------------------------

    def _item(self, inspection: _Inspection) -> dict:
        digest, refusal = inspection.digest, inspection.refusal
        earlier = self._attempts.refusal_for(digest) if digest is not None else None
        if earlier is not None:
            digest, refusal = None, earlier
        return {"session_id": inspection.session_id, "capture": inspection.capture, "digest": digest,
                "refusal": refusal.value if refusal else None}

    def _inspect(self, session_id: str) -> _Inspection:
        """Pane, process, then the screen, then pane and process again (a
        change during the capture is a refusal)."""
        if not self.enrollment.is_enrolled(session_id, strict=True):
            return _Inspection(session_id, None, None, None, StartupRefusal.NOT_ENROLLED)
        try:
            pane = self._pane(session_id)
            if isinstance(pane, StartupRefusal):
                return _Inspection(session_id, None, None, None, pane)
            identity = self._identity(session_id, pane)
            capture = self._capture_live(pane.pane_id)
            if capture is None:
                return _Inspection(session_id, None, None, None, StartupRefusal.NO_WINDOW)
            selection = desk_startup_selection(capture)
            if isinstance(identity, StartupRefusal):
                return _Inspection(session_id, capture, selection, None, identity)
            again = self._pane(session_id)
            if again != pane or self._identity(session_id, again) != identity:
                return _Inspection(session_id, capture, selection, identity, StartupRefusal.CHANGED)
            refusal = _screen_refusal(selection)
            digest = view_digest(identity, capture, self.epoch) if refusal is None else None
            return _Inspection(session_id, capture, selection, identity, refusal, digest)
        except _TMUX_ERRORS:
            return _Inspection(session_id, None, None, None, StartupRefusal.TMUX_FAILED)

    def _capture_live(self, pane_id: str) -> str | None:
        """The pane's screen, or None when tmux's error is explained by the
        pane being gone (it is no longer listed). Any other failure raises."""
        try:
            return self.tmux.capture(pane_id, check=True)
        except TmuxError:
            if any(pane.pane_id == pane_id for pane in self.tmux.panes()):
                raise
            return None

    def _pane(self, session_id: str) -> Pane | StartupRefusal:
        """The supervisor's window for the session: the one tagged with its
        id (`Tmux.start` sets @car_session, and the supervisor matches its
        windows by it), holding exactly one live pane."""
        panes = [pane for pane in self.tmux.panes() if pane.session_id == session_id]
        windows = {pane.window_id for pane in panes}
        if not windows:
            return StartupRefusal.NO_WINDOW
        if len(windows) > 1:
            return StartupRefusal.AMBIGUOUS_WINDOW
        if len(panes) != 1 or panes[0].dead or panes[0].pane_pid is None:
            return StartupRefusal.PANE_LAYOUT
        if panes[0].in_mode:
            return StartupRefusal.PANE_IN_MODE
        return panes[0]

    def _identity(self, session_id: str, pane: Pane) -> LaunchIdentity | StartupRefusal:
        proc = self.config.proc_dir
        # Without a mounted process tree nothing can be told apart.
        if not (proc / "self").is_dir():
            return StartupRefusal.UNVERIFIABLE
        pane_process = read_process_entry(proc, pane.pane_pid)
        if not isinstance(pane_process, ProcessRecord):
            return _UNREADABLE_PROCESS[pane_process]
        children = self._children(pane_process.pid)
        if isinstance(children, StartupRefusal):
            return children
        versions_dir = Path(os.path.realpath(self.config.claude_versions_dir))
        claude = foreground_claude(pane_process, children, versions_dir)
        if isinstance(claude, StartupRefusal):
            return claude
        if managed_for(proc / str(claude.pid)) != session_id:
            return StartupRefusal.NOT_SUPERVISED
        refusal = check_launch_argv(claude.argv, session_id, self.config)
        if refusal is None:
            entry = read_session_entry(self.config.claude_sessions_dir, claude.pid)
            refusal = entry if isinstance(entry, StartupRefusal) else check_session_file(*entry, session_id)
        if refusal is not None:
            return refusal
        return LaunchIdentity(session_id=session_id, window_id=pane.window_id, pane_id=pane.pane_id,
                              pane_pid=pane_process.pid, pane_start=pane_process.start,
                              claude_pid=claude.pid, claude_start=claude.start)

    def _children(self, pid: int) -> list[ProcessRecord] | StartupRefusal:
        """Every process whose parent is `pid`. Parentage comes first, from
        each entry's `stat` alone; only children get the full read, so an
        unrelated process's command line or name can't stop the scan. An
        entry whose parent can't be read might be a child (a second claude
        must not hide), so it refuses, as does a child that can't be read;
        one that exits during the scan is skipped."""
        proc = self.config.proc_dir
        found = []
        with os.scandir(proc) as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                ppid = read_parent_pid(proc, int(entry.name))
                if ppid is ChannelProcess.UNVERIFIABLE:
                    return StartupRefusal.UNVERIFIABLE
                if ppid != pid:
                    continue
                record = read_process_entry(proc, int(entry.name))
                if record is ChannelProcess.UNVERIFIABLE:
                    return StartupRefusal.UNVERIFIABLE
                # The full read is a second look: the pid may have exited, or been reused, since.
                if isinstance(record, ProcessRecord) and record.ppid == pid:
                    found.append(record)
        return found


# The pane's own process: gone means nothing runs there; unreadable means unknown.
_UNREADABLE_PROCESS = {ChannelProcess.GONE: StartupRefusal.NO_FOREGROUND,
                       ChannelProcess.UNVERIFIABLE: StartupRefusal.UNVERIFIABLE}


def _pane_guard(identity: LaunchIdentity) -> PaneGuard:
    """What tmux re-tests in the command that presses the key: the pane the
    checks saw, still the session's only pane, live, taking input, and
    showing the warning with the accept option selected."""
    return PaneGuard(
        expect=(("pane_id", identity.pane_id), ("pane_pid", str(identity.pane_pid)),
                ("window_id", identity.window_id), ("@car_session", identity.session_id),
                ("window_panes", "1"), ("pane_dead", "0"), ("pane_in_mode", "0"), ("pane_input_off", "0")),
        present=DESK_WARNING_GUARD_PRESENT, absent=DESK_WARNING_GUARD_ABSENT)


def _screen_refusal(selection: int | None) -> StartupRefusal | None:
    if selection is None:
        return StartupRefusal.NOT_BLOCKED
    if selection != DESK_WARNING_ACCEPT_OPTION:
        return StartupRefusal.EXIT_SELECTED
    return None


def _check_session_id(session_id) -> None:
    if not isinstance(session_id, str) or not is_session_id(session_id):
        raise DeskError(400, "bad session_id")
