"""macOS Claude registry liveness, without /proc or a supervisor."""

import json
import os
from pathlib import Path

from .live_registry import LiveProcess
from .paths import LIVE_REGISTRY_DIR
from .host_protocol import SessionState, session_id


def mac_live_processes(registry_dir: Path = LIVE_REGISTRY_DIR, kill=os.kill) -> list[LiveProcess]:
    found = []
    for path in Path(registry_dir).glob("*.json"):
        try:
            raw = json.loads(path.read_text())
            pid = raw["pid"]
            if type(pid) is not int or pid <= 0:
                continue
            sid = session_id(raw.get("sessionId"))
            try:
                kill(pid, 0)
            except PermissionError:
                pass  # Still alive; err on the side of refusing edits.
            state = SessionState.BUSY if raw.get("status") == "busy" else SessionState.IDLE
            found.append(LiveProcess(pid, sid, str(raw.get("cwd", "")), state, None, None))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return found
