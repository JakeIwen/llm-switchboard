"""The startup acknowledgement: show an enrolled session's development-channel
warning and, when Jacob taps the button (or confirms `desk ack`), press Enter
once in its pane (docs/voice-desk.md, "Startup acknowledgement").

The hub sends the Enter itself, over the dedicated tmux server's socket,
rather than asking the supervisor: the supervisor runs in the picker's
process with no authenticated path from the hub, and keys typed into a
supervisor window from outside its loop already have a precedent (the
picker types `/rename`). Only the supervisor starts or kills windows.

Lock order (docs/voice-desk.md, "Hub state"): an acknowledgement takes its
own `_sending` lock without waiting (another one in progress is refused),
then `enrolled.json`'s lock, first in the hub's order and re-entrant in this
process. It holds that file lock across its tmux calls and /proc reads, so
an unenroll that has returned is never overtaken by an Enter that read the
enrollment before it, and a second acknowledgement can't interleave. It
never takes the policy lock, and takes the state's mutex only inside the
token check (`authorize`), never across a tmux call or a lock wait. Every
tmux call times out after STARTUP_TMUX_TIMEOUT_SECONDS, which bounds how
long housekeeping and a send's handoff wait for that file lock.

What stays non-atomic: tmux has no "send only if the screen still reads
this", so between the last check (a capture, then the pane and the process
read again) and `send-keys` Claude could redraw or the process could exit
and the pane be respawned. The window is milliseconds; Claude is waiting for
this key, and the supervisor alone respawns panes, always for the same
session. The digest covers the process as well as the screen, so a view
taken before a respawn never authorizes an Enter after it.

Logs carry session ids and refusal reasons only, never screen content.
"""

from collections import deque
from dataclasses import dataclass
import logging
import os
import subprocess
import threading
import time
from typing import Callable

from ..live_registry import managed_for
from ..screen import DESK_WARNING_ACCEPT_OPTION, desk_startup_selection
from ..session_id import is_session_id
from ..tmux import Pane, Tmux, TmuxError
from .config import DeskConfig
from .enrollment import Enrollment
from .model import (
    MAX_STARTUP_ACKS_PER_MINUTE, MAX_STARTUP_VIEWS_PER_MINUTE, SHA256_HEX_PATTERN, DeskError,
)
from .peer import ProcessRecord, read_process, read_session_file
from .startup_identity import (
    LaunchIdentity, StartupRefusal, check_launch_argv, check_session_file, foreground_claude, refused,
    view_digest,
)

log = logging.getLogger("llm_switchboard.desk")

_RATE_WINDOW_SECONDS = 60


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


@dataclass(frozen=True)
class _Inspection:
    session_id: str
    capture: str | None
    # The warning's selected option, or None when the screen isn't the warning.
    selection: int | None
    identity: LaunchIdentity | None
    refusal: StartupRefusal | None

    @property
    def digest(self) -> str | None:
        if self.refusal is not None or self.identity is None or self.capture is None:
            return None
        return view_digest(self.identity, self.capture)

    def item(self) -> dict:
        return {"session_id": self.session_id, "capture": self.capture, "digest": self.digest,
                "refusal": self.refusal.value if self.refusal else None}


class StartupAck:
    def __init__(self, config: DeskConfig, enrollment: Enrollment, tmux: Tmux,
                 now: Callable[[], float] = time.monotonic):
        self.config = config
        self.enrollment = enrollment
        self.tmux = tmux
        self._views = _RateWindow(MAX_STARTUP_VIEWS_PER_MINUTE, now)
        self._acks = _RateWindow(MAX_STARTUP_ACKS_PER_MINUTE, now)
        self._sending = threading.Lock()

    # -- public entry points ---------------------------------------------------

    def blocked(self) -> list[dict]:
        """Every enrolled session whose supervisor pane shows the warning, with
        its capture and, when it may be acknowledged, the digest to send back."""
        if not self._views.take():
            raise refused(StartupRefusal.TOO_MANY)
        with_panes = {pane.session_id for pane in self._panes()}
        found = []
        for session_id in sorted(set(self.enrollment.sessions()) & with_panes):
            inspection = self._inspect(session_id)
            if inspection.selection is not None:
                found.append(inspection.item())
        return found

    def view(self, session_id) -> dict:
        """One session's warning, for `desk ack`; refused when it isn't showing."""
        _check_session_id(session_id)
        if not self._views.take():
            raise refused(StartupRefusal.TOO_MANY)
        inspection = self._inspect(session_id)
        if inspection.selection is None:
            raise refused(inspection.refusal or StartupRefusal.NOT_BLOCKED)
        return inspection.item()

    def acknowledge(self, session_id, digest, authorize: Callable[[], None]) -> dict:
        """One Enter in the session's pane, only if everything the digest was
        issued for still holds. `authorize` raises (401) when the caller's
        token stopped being valid; the operator socket passes a no-op."""
        _check_session_id(session_id)
        if not isinstance(digest, str) or not SHA256_HEX_PATTERN.fullmatch(digest):
            raise DeskError(400, "bad digest")
        if not self._acks.take():
            raise refused(StartupRefusal.TOO_MANY)
        if not self._sending.acquire(blocking=False):
            raise refused(StartupRefusal.BUSY)
        try:
            with self.enrollment.locked():
                pane_id = self._final_check(session_id, digest)
                authorize()
                self._send_enter(session_id, pane_id)
        except DeskError as exc:
            log.info("desk: startup ack for session %s refused: %s", session_id[:8], exc.reason)
            raise
        finally:
            self._sending.release()
        log.info("desk: startup ack for session %s: Enter sent", session_id[:8])
        return {"session_id": session_id, "sent": True}

    # -- checks ------------------------------------------------------------------

    def _final_check(self, session_id: str, digest: str) -> str:
        """Inspect twice: the first answers why a stale view fails, the second
        is the recheck immediately before the key and must see the same
        process and the same screen. Returns the pane to send to."""
        first = self._inspect(session_id)
        if first.refusal is not None:
            raise refused(first.refusal)
        if first.digest != digest:
            raise refused(StartupRefusal.DIGEST_MISMATCH)
        final = self._inspect(session_id)
        if final.refusal is not None:
            raise refused(final.refusal)
        if final.identity != first.identity or final.digest != digest:
            raise refused(StartupRefusal.CHANGED)
        return final.identity.pane_id

    def _send_enter(self, session_id: str, pane_id: str) -> None:
        try:
            self.tmux.send_enter(pane_id)
        except (TmuxError, subprocess.SubprocessError, OSError) as exc:
            # A timed-out send-keys may still have delivered the key.
            log.warning("desk: startup ack for session %s: send-keys failed (%s)", session_id[:8],
                        type(exc).__name__)
            raise DeskError(503, "tmux did not confirm the Enter; check the session") from None

    def _inspect(self, session_id: str) -> _Inspection:
        """Pane, process, then the screen, then pane and process again (a
        change during the capture is a refusal)."""
        if not self.enrollment.is_enrolled(session_id):
            return _Inspection(session_id, None, None, None, StartupRefusal.NOT_ENROLLED)
        try:
            pane = self._pane(session_id)
            if isinstance(pane, StartupRefusal):
                return _Inspection(session_id, None, None, None, pane)
            identity = self._identity(session_id, pane)
            capture = self.tmux.capture(pane.pane_id)
            selection = desk_startup_selection(capture)
            if isinstance(identity, StartupRefusal):
                return _Inspection(session_id, capture, selection, None, identity)
            again = self._pane(session_id)
            if again != pane or self._identity(session_id, again) != identity:
                return _Inspection(session_id, capture, selection, identity, StartupRefusal.CHANGED)
            return _Inspection(session_id, capture, selection, identity, _screen_refusal(selection))
        except (TmuxError, subprocess.SubprocessError, OSError):
            return _Inspection(session_id, None, None, None, StartupRefusal.TMUX_FAILED)

    def _panes(self) -> list[Pane]:
        try:
            return self.tmux.panes()
        except (TmuxError, subprocess.SubprocessError, OSError):
            raise refused(StartupRefusal.TMUX_FAILED) from None

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
        pane_process = read_process(proc, pane.pane_pid)
        if pane_process is None:
            return StartupRefusal.NO_FOREGROUND
        claude = foreground_claude(pane_process, self._children(pane_process.pid))
        if isinstance(claude, StartupRefusal):
            return claude
        if managed_for(proc / str(claude.pid)) != session_id:
            return StartupRefusal.NOT_SUPERVISED
        refusal = check_launch_argv(claude.argv, session_id, self.config)
        if refusal is None:
            session_file = self.config.claude_sessions_dir / f"{claude.pid}.json"
            refusal = check_session_file(os.path.lexists(session_file),
                                         read_session_file(self.config.claude_sessions_dir, claude.pid), session_id)
        if refusal is not None:
            return refusal
        return LaunchIdentity(session_id=session_id, window_id=pane.window_id, pane_id=pane.pane_id,
                              pane_pid=pane_process.pid, pane_start=pane_process.start,
                              claude_pid=claude.pid, claude_start=claude.start)

    def _children(self, pid: int) -> list[ProcessRecord]:
        found = []
        with os.scandir(self.config.proc_dir) as entries:
            for entry in entries:
                if entry.name.isdigit():
                    record = read_process(self.config.proc_dir, int(entry.name))
                    if record is not None and record.ppid == pid:
                        found.append(record)
        return found


def _screen_refusal(selection: int | None) -> StartupRefusal | None:
    if selection is None:
        return StartupRefusal.NOT_BLOCKED
    if selection != DESK_WARNING_ACCEPT_OPTION:
        return StartupRefusal.EXIT_SELECTED
    return None


def _check_session_id(session_id) -> None:
    if not isinstance(session_id, str) or not is_session_id(session_id):
        raise DeskError(400, "bad session_id")
