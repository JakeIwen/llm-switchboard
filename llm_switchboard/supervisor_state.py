"""Supervisor state records and pure terminal-screen decisions."""

from dataclasses import dataclass
from enum import Enum

from .screen import Rc, blocking_prompt, rc_indicator

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


@dataclass(frozen=True)
class ScreenInput:
    screen: str
    running_s: float
    duplicate_pid: int | None = None


@dataclass(frozen=True)
class ScreenObservation:
    state: State
    detail: str
    rc_active: bool


def backoff_s(failures: int) -> float:
    if failures <= 0:
        return 0.0
    return min(BACKOFF_BASE_S * 2 ** (failures - 1), BACKOFF_MAX_S)


def observe_screen(observation: ScreenInput) -> ScreenObservation:
    """Describe a running pane from captured text and elapsed run time."""
    rc = rc_indicator(observation.screen)
    prompt = blocking_prompt(observation.screen)
    if prompt:
        state, detail = State.ATTENTION, prompt
    elif rc == Rc.ACTIVE:
        state, detail = State.RUNNING, "Remote Control active."
    elif rc == Rc.FAILED:
        state, detail = State.ATTENTION, "Remote Control failed to connect; try Restart."
    elif rc in (Rc.CONNECTING, Rc.RECONNECTING):
        state, detail = State.STARTING, f"Remote Control {rc.value}."
    elif observation.running_s < RC_GRACE_S:
        state, detail = State.STARTING, "Launched; waiting for Remote Control."
    else:
        state = State.ATTENTION
        detail = "Running, but no Remote Control indicator on its status line. Check the screen."
    if observation.duplicate_pid is not None:
        detail += f" Also open in another claude process (pid {observation.duplicate_pid})."
    return ScreenObservation(state, detail, rc == Rc.ACTIVE)
