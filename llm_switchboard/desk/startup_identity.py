"""Launch identity for the startup acknowledgement (docs/voice-desk.md,
"Startup acknowledgement").

An enrolled session waits at Claude Code's development-channel warning
before it opens the session: on 2.1.330 there is no `sessions/<pid>.json`
yet, so the channel identity in `peer.py` (the session file names the
session) can't apply. Jacob approved (2026-11-22) a weaker identity for this
one Enter: the supervisor's own window for the session, holding a claude
process the supervisor launched for it with the pinned desk flags and the
session id as the exact value of `--resume` or `--session-id`, and no
session file naming another session. It identifies the *requested* session,
not one Claude has opened: a resume of a bogus id reaches the same warning,
and the Enter then opens an empty or failed session (the accepted risk).

Pure rules over parsed records; `startup_ack.py` reads tmux and /proc.
Standard library only.
"""

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import os

from .config import DeskConfig
from .model import DeskError
from .peer import ProcessRecord, has_channel_enablement, has_mcp_config_flag


class StartupRefusal(str, Enum):
    """Why a session can't be acknowledged; the value is the client-facing reason."""
    NOT_ENROLLED = "the session is not enrolled"
    NO_WINDOW = "no supervisor window for this session"
    AMBIGUOUS_WINDOW = "more than one window claims this session"
    PANE_LAYOUT = "the session's window does not hold exactly one live pane"
    PANE_IN_MODE = "the pane is in tmux copy mode"
    NO_FOREGROUND = "no foreground process in the session's pane"
    AMBIGUOUS_PROCESS = "more than one claude process in the session's pane"
    NOT_CLAUDE = "the pane's foreground process is not claude"
    NOT_SUPERVISED = "claude was not launched by the supervisor for this session"
    MISSING_FLAG = "claude was not launched with the pinned desk flags"
    WRONG_SESSION = "claude was not launched for exactly this session"
    SESSION_FILE_MISMATCH = "claude's session file names another session"
    NOT_BLOCKED = "the session is not at the startup warning"
    EXIT_SELECTED = "the warning has Exit selected; attach to choose"
    CHANGED = "the screen or the process changed; view it again"
    DIGEST_MISMATCH = "the screen changed since it was shown; view it again"
    UNVERIFIABLE = "the session's process can't be checked"
    TMUX_FAILED = "tmux did not answer"
    BUSY = "another startup acknowledgement is in progress"
    TOO_MANY = "too many startup requests; wait a minute"


# 409: the session's state doesn't allow it; 503: nothing could be checked;
# 429: the bound on acknowledgements and views.
_STATUS = {refusal: 409 for refusal in StartupRefusal} | {
    StartupRefusal.UNVERIFIABLE: 503,
    StartupRefusal.TMUX_FAILED: 503,
    StartupRefusal.BUSY: 429,
    StartupRefusal.TOO_MANY: 429,
}


def refused(refusal: StartupRefusal) -> DeskError:
    return DeskError(_STATUS[refusal], refusal.value)


# Options that choose which conversation claude opens. Exactly one may
# appear, and it must be one of SESSION_OPTIONS followed by the session id.
SESSION_OPTIONS = frozenset({"--resume", "--session-id"})
_OTHER_SESSION_OPTIONS = frozenset({"-r", "-c", "--continue", "--fork-session", "--from-pr"})


@dataclass(frozen=True)
class LaunchIdentity:
    """What the acknowledgement checked, compared again before the Enter."""
    session_id: str
    window_id: str
    pane_id: str
    pane_pid: int
    pane_start: str
    claude_pid: int
    claude_start: str


def view_digest(identity: LaunchIdentity, capture: str) -> str:
    """Binds the screen Jacob was shown to the process it was shown for, so a
    respawned pane with an identical screen still needs a fresh view."""
    payload = json.dumps({"capture": capture, **asdict(identity)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def is_claude(process: ProcessRecord) -> bool:
    return bool(process.argv) and os.path.basename(process.argv[0]) == "claude"


def foreground_claude(pane: ProcessRecord, children: list[ProcessRecord]) -> ProcessRecord | StartupRefusal:
    """The pane's foreground process group leader, when it is the pane
    program's one claude child."""
    if pane.tpgid <= 0:
        return StartupRefusal.NO_FOREGROUND
    claudes = [child for child in children if is_claude(child)]
    if len(claudes) > 1:
        return StartupRefusal.AMBIGUOUS_PROCESS
    leader = [child for child in children if child.pid == pane.tpgid and child.pgrp == pane.tpgid]
    if len(leader) != 1:
        return StartupRefusal.NO_FOREGROUND
    return leader[0] if claudes == leader else StartupRefusal.NOT_CLAUDE


def check_launch_argv(argv: tuple[str, ...], session_id: str, config: DeskConfig) -> StartupRefusal | None:
    """The pinned flags as adjacent pairs, and the session id as the exact
    value of the one session-choosing option (never a substring of one)."""
    if not has_mcp_config_flag(argv, config.mcp_config) or not has_channel_enablement(argv, config):
        return StartupRefusal.MISSING_FLAG
    choosers = [i for i, arg in enumerate(argv) if arg in SESSION_OPTIONS or arg in _OTHER_SESSION_OPTIONS
                or arg.startswith(("--resume=", "--session-id="))]
    if len(choosers) != 1:
        return StartupRefusal.WRONG_SESSION
    at = choosers[0]
    if argv[at] not in SESSION_OPTIONS or at + 1 >= len(argv) or argv[at + 1] != session_id:
        return StartupRefusal.WRONG_SESSION
    return None


def check_session_file(present: bool, entry: dict | None, session_id: str) -> StartupRefusal | None:
    """A missing file is expected at the warning. One that exists must be
    readable and name this session."""
    if not present:
        return None
    if entry is None or entry.get("sessionId") != session_id:
        return StartupRefusal.SESSION_FILE_MISMATCH
    return None
