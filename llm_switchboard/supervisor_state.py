"""Supervisor state records and pure terminal-screen decisions."""

from dataclasses import dataclass
from enum import Enum

from .screen import Rc, blocking_prompt, desk_startup_warning, rc_indicator

RC_GRACE_S = 45
# A run at least this long counts as healthy and resets the backoff.
STABLE_RUN_S = 300
BACKOFF_BASE_S = 10
BACKOFF_MAX_S = 600
SCREEN_TAIL_LINES = 40
# A session that sits idle without Remote Control is restarted on its own after
# this long, then after twice as long each time Remote Control still doesn't
# come back, up to the cap. A restart is cheap and is the usual fix.
NO_RC_RESTART_S = 600
NO_RC_RESTART_MAX_S = 3600


class State(str, Enum):
    RUNNING = "running"  # window up, Remote Control connected
    STARTING = "starting"  # window up, Remote Control not seen yet
    STARTUP_BLOCKED = "startup_blocked"  # Claude is waiting at the desk channel warning
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
    # When Remote Control was last seen missing with nothing on screen to
    # answer, and how many automatic restarts haven't brought it back.
    no_rc_since: float | None = None
    auto_restarts: int = 0


@dataclass(frozen=True)
class ScreenInput:
    screen: str
    running_s: float
    duplicate_pid: int | None = None
    # The process's registry entry says Remote Control is on. Claude Code
    # 2.1.325 stopped putting the indicator on the status line, so the
    # screen alone no longer shows a connected session.
    rc_registered: bool = False


@dataclass(frozen=True)
class ScreenObservation:
    state: State
    detail: str
    rc_active: bool
    # Remote Control is off and a restart, not an answer on screen, is the fix.
    rc_lost: bool = False


def backoff_s(failures: int) -> float:
    if failures <= 0:
        return 0.0
    return min(BACKOFF_BASE_S * 2 ** (failures - 1), BACKOFF_MAX_S)


def no_rc_restart_delay_s(auto_restarts: int) -> float:
    return min(NO_RC_RESTART_S * 2 ** auto_restarts, NO_RC_RESTART_MAX_S)


def observe_screen(observation: ScreenInput) -> ScreenObservation:
    """Describe a running pane from captured text and elapsed run time."""
    if desk_startup_warning(observation.screen):
        return ScreenObservation(State.STARTUP_BLOCKED,
                                 "Development channel warning; attach to acknowledge it before Remote Control starts.",
                                 rc_active=False)
    rc = rc_indicator(observation.screen)
    prompt = blocking_prompt(observation.screen)
    lost = False
    if prompt:
        state, detail = State.ATTENTION, prompt
    elif rc == Rc.ACTIVE:
        state, detail = State.RUNNING, "Remote Control active."
    elif rc == Rc.FAILED:
        state, detail = State.ATTENTION, "Remote Control failed to connect; try Restart."
        lost = True
    elif rc in (Rc.CONNECTING, Rc.RECONNECTING):
        state, detail = State.STARTING, f"Remote Control {rc.value}."
    elif observation.rc_registered:
        state, detail = State.RUNNING, "Remote Control active."
    elif observation.running_s < RC_GRACE_S:
        state, detail = State.STARTING, "Launched; waiting for Remote Control."
    else:
        state = State.ATTENTION
        detail = ("Running, but Remote Control is off (no indicator on its status line). "
                  "Restart usually brings it back; also quit any copy of this session opened without claudepd.")
        # A blank capture (tmux failed to read the pane) proves nothing.
        lost = bool(observation.screen.strip())
    if observation.duplicate_pid is not None:
        detail += f" Also open in another claude process (pid {observation.duplicate_pid})."
    active = rc == Rc.ACTIVE or (rc is None and observation.rc_registered)
    return ScreenObservation(state, detail, active, lost)
