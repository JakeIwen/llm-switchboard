"""Which Claude Code sessions are open right now, read from ~/.claude/sessions.

Every running claude process writes <pid>.json there. Entries outlive crashed
processes, so each one is checked against /proc (pid alive and same start
time) before it counts.
"""

from dataclasses import dataclass
from pathlib import Path
import json

from .paths import LIVE_REGISTRY_DIR, MANAGED_ENV_VAR


@dataclass(frozen=True)
class LiveProcess:
    pid: int
    session_id: str
    cwd: str
    # "idle" or "busy", as Claude Code reports it.
    status: str
    # The session id we launched this process for, or None if we didn't.
    managed_for: str | None
    rss_kb: int | None


def live_processes(registry_dir: Path = LIVE_REGISTRY_DIR, proc_dir: Path = Path("/proc")) -> list[LiveProcess]:
    found = []
    for entry_path in Path(registry_dir).glob("*.json"):
        try:
            entry = json.loads(entry_path.read_text())
            pid = int(entry["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        proc = Path(proc_dir) / str(pid)
        if not _same_process(proc, entry.get("procStart")):
            continue
        found.append(
            LiveProcess(
                pid=pid,
                session_id=str(entry.get("sessionId", "")),
                cwd=str(entry.get("cwd", "")),
                status=str(entry.get("status", "")),
                managed_for=_managed_for(proc),
                rss_kb=_rss_kb(proc),
            )
        )
    return found


def _same_process(proc: Path, proc_start) -> bool:
    try:
        stat = (proc / "stat").read_text()
    except OSError:
        return False
    if proc_start is None:
        return True
    # Field 22 is the start time; split after the ")" that closes the command
    # name, which may itself contain spaces.
    fields = stat.rsplit(")", 1)[1].split()
    return fields[19] == str(proc_start)


def _managed_for(proc: Path) -> str | None:
    try:
        environ = (proc / "environ").read_bytes()
    except OSError:
        return None
    prefix = MANAGED_ENV_VAR.encode() + b"="
    for item in environ.split(b"\0"):
        if item.startswith(prefix):
            return item[len(prefix):].decode(errors="replace") or None
    return None


def _rss_kb(proc: Path) -> int | None:
    try:
        for line in (proc / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None
