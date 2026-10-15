"""Keeps every enabled session running in its tmux window.

One loop owns tmux: it reads the store, starts a window for each enabled
session that has none, relaunches sessions that exited (with backoff), and
closes windows for sessions that were disabled. The web server and CLI only
edit the store and ask for a pass with wake().
"""

from dataclasses import dataclass, field, replace
from enum import Enum
import logging
import os
import threading
import time

from .connectivity import Connectivity
from .launcher import pane_command
from .live_registry import LiveProcess, live_processes
from .paths import MANAGED_ENV_VAR
from .session_id import short_id
from .store import Store
from .tmux import Tmux, TmuxError
from .transcripts import TranscriptIndex

log = logging.getLogger(__name__)

# Shown in Claude Code's status line once Remote Control is connected.
RC_MARKERS = ("/rc active", "/remote-control is active")
# Prompts that block startup until someone answers them in the terminal.
BLOCKING_PROMPTS = {
    "trust this folder": "Folder trust prompt is waiting; attach to answer it.",
    "Enter to confirm": "A prompt is waiting for input; attach to answer it.",
}
RC_GRACE_S = 45
# A run at least this long counts as healthy and resets the backoff.
STABLE_RUN_S = 300
BACKOFF_BASE_S = 10
BACKOFF_MAX_S = 600
SCREEN_TAIL_LINES = 40


class State(str, Enum):
    RUNNING = "running"  # window up, Remote Control connected
    STARTING = "starting"  # window up, Remote Control not seen yet
    ATTENTION = "attention"  # window up, but something on screen needs a human
    ELSEWHERE = "elsewhere"  # open in another claude process; waits for it to close
    OFFLINE = "offline"  # no route to the API; waits for the network
    BACKOFF = "backoff"  # exited; relaunch scheduled
    MISSING = "missing"  # no transcript with this id
    ERROR = "error"  # tmux itself failed


@dataclass(frozen=True)
class Exit:
    status: int | None
    at: float
    ran_s: float
    screen: str


@dataclass
class Status:
    session_id: str
    state: State = State.STARTING
    detail: str = ""
    window_id: str | None = None
    pid: int | None = None
    rss_kb: int | None = None
    rc_active: bool = False
    busy: bool = False
    # Another claude process has the same session open as well.
    duplicate: bool = False
    started_at: float | None = None
    next_attempt: float = 0.0
    failures: int = 0
    launches: int = 0
    last_exit: Exit | None = None
    rc_name: str = ""


@dataclass
class _Requests:
    restart: set[str] = field(default_factory=set)


def backoff_s(failures: int) -> float:
    if failures <= 0:
        return 0.0
    return min(BACKOFF_BASE_S * 2 ** (failures - 1), BACKOFF_MAX_S)


class Supervisor:
    def __init__(
        self,
        store: Store,
        index: TranscriptIndex,
        tmux: Tmux,
        connectivity: Connectivity,
        registry=live_processes,
        clock=time.time,
        interval_s: float = 5.0,
    ):
        self.store = store
        self.index = index
        self.tmux = tmux
        self.connectivity = connectivity
        self.registry = registry
        self.clock = clock
        self.interval_s = interval_s
        self._statuses: dict[str, Status] = {}
        self._requests = _Requests()
        self._lock = threading.Lock()
        self._wake = threading.Event()

    # --- called from other threads ------------------------------------

    def wake(self) -> None:
        self._wake.set()

    def request_restart(self, session_id: str) -> None:
        with self._lock:
            self._requests.restart.add(session_id)
        self.wake()

    def snapshot(self) -> dict[str, Status]:
        with self._lock:
            return {sid: replace(s) for sid, s in self._statuses.items()}

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile pass failed")
            self._wake.wait(self.interval_s)
            self._wake.clear()

    # --- the loop -------------------------------------------------------

    def reconcile(self) -> None:
        now = self.clock()
        enabled = {sid: e for sid, e in self.store.load().items() if e.enabled}
        with self._lock:
            restarts, self._requests.restart = self._requests.restart, set()
        windows = {w.session_id: w for w in self.tmux.windows()}
        live = self.registry()

        for sid, window in list(windows.items()):
            if sid not in enabled or sid in restarts:
                log.info("closing window for %s (%s)", short_id(sid), "restart" if sid in enabled else "disabled")
                self.tmux.kill(window.window_id)
                del windows[sid]

        statuses = {}
        for sid, entry in enabled.items():
            status = self._statuses.get(sid) or Status(session_id=sid)
            if sid in restarts:
                status.failures, status.next_attempt = 0, 0.0
            try:
                self._reconcile_one(status, entry.name, windows.get(sid), live, now)
            except TmuxError as e:
                log.error("tmux failed for %s: %s", short_id(sid), e)
                status.state, status.detail = State.ERROR, str(e)
                status.failures += 1
                status.next_attempt = now + backoff_s(status.failures)
            statuses[sid] = status
        with self._lock:
            self._statuses = statuses

    def _reconcile_one(self, status: Status, rc_name: str, window, live: list[LiveProcess], now: float) -> None:
        sid = status.session_id
        others = [p for p in live if p.session_id == sid and p.managed_for != sid]
        ours = next((p for p in live if p.managed_for == sid), None)

        if window and window.dead:
            self._record_exit(status, window, now)
            window = None

        if window:
            self._observe(status, window, ours, others, now)
            return

        status.window_id = status.pid = status.rss_kb = None
        status.rc_active = status.busy = status.duplicate = False
        if others:
            status.state = State.ELSEWHERE
            status.detail = f"Open in another claude process (pid {others[0].pid}); starts when that one exits."
            return
        transcript = self.index.get(sid)
        if transcript is None:
            status.state, status.detail = State.MISSING, "No transcript with this session id."
            return
        if now < status.next_attempt:
            status.state = State.BACKOFF
            status.detail = f"Relaunching in {int(status.next_attempt - now)}s."
            return
        if not self.connectivity.ok():
            status.state, status.detail = State.OFFLINE, "Waiting for api.anthropic.com to be reachable."
            return

        status.rc_name = rc_name or transcript.title or os.path.basename(transcript.cwd) or short_id(sid)
        command = pane_command(sid, transcript.cwd, status.rc_name)
        label = _window_label(transcript.project, sid)
        log.info("launching %s in %s as %r", short_id(sid), transcript.cwd, status.rc_name)
        status.window_id = self.tmux.start(sid, label, command, {MANAGED_ENV_VAR: sid})
        status.started_at = now
        status.launches += 1
        status.state, status.detail = State.STARTING, "Launched; waiting for Remote Control."

    def _observe(self, status: Status, window, ours: LiveProcess | None, others: list[LiveProcess], now: float) -> None:
        status.window_id = window.window_id
        if status.started_at is None:
            # Already running when this supervisor started (service restart).
            status.started_at = now - RC_GRACE_S
        status.pid = ours.pid if ours else None
        status.rss_kb = ours.rss_kb if ours else None
        status.busy = bool(ours and ours.status == "busy")
        status.duplicate = bool(others)
        screen = self.tmux.capture(window.window_id)
        status.rc_active = any(m in screen for m in RC_MARKERS)
        prompt = next((msg for text, msg in BLOCKING_PROMPTS.items() if text in screen), None)
        if status.rc_active and not prompt:
            status.state, status.detail = State.RUNNING, "Remote Control active."
        elif prompt:
            status.state, status.detail = State.ATTENTION, prompt
        elif now - status.started_at < RC_GRACE_S:
            status.state, status.detail = State.STARTING, "Launched; waiting for Remote Control."
        else:
            status.state = State.ATTENTION
            status.detail = "Running, but Remote Control isn't showing as active. Check the screen."
        if status.duplicate:
            status.detail += f" Also open in another claude process (pid {others[0].pid})."

    def _record_exit(self, status: Status, window, now: float) -> None:
        screen = self.tmux.capture(window.window_id, history=SCREEN_TAIL_LINES)
        tail = "\n".join(screen.rstrip().splitlines()[-SCREEN_TAIL_LINES:])
        ran_s = now - status.started_at if status.started_at is not None else 0.0
        status.last_exit = Exit(status=window.exit_status, at=now, ran_s=ran_s, screen=tail)
        status.failures = 0 if ran_s >= STABLE_RUN_S else status.failures + 1
        # Even a healthy run gets a short pause so a session that exits on
        # purpose isn't relaunched in the same second.
        status.next_attempt = now + max(backoff_s(status.failures), 3.0)
        status.started_at = None
        log.warning(
            "%s exited with status %s after %ds; relaunch in %ds",
            short_id(status.session_id), window.exit_status, ran_s, status.next_attempt - now,
        )
        self.tmux.kill(window.window_id)


def _window_label(project: str, session_id: str) -> str:
    name = project.rstrip("/").rsplit("/", 1)[-1] or "home"
    return f"{name}-{short_id(session_id)[:4]}"
