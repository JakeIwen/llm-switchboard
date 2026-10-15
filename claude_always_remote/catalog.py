"""One row per session for the picker and the CLI: what's on disk, whether it's
enabled, whether it's open anywhere, and what the supervisor is doing with it."""

from dataclasses import asdict

from .launcher import resume_command
from .live_registry import LiveProcess
from .session_id import short_id
from .store import Entry
from .supervisor import Status
from .transcripts import Transcript, display_path


def session_rows(
    transcripts: list[Transcript],
    entries: dict[str, Entry],
    live: list[LiveProcess],
    statuses: dict[str, Status],
) -> list[dict]:
    by_id = {t.session_id: t for t in transcripts}
    ids = list(by_id) + [sid for sid in entries if sid not in by_id]
    rows = [_row(sid, by_id.get(sid), entries.get(sid), live, statuses.get(sid)) for sid in ids]
    # Enabled sessions first, then most recently active.
    rows.sort(key=lambda r: (not r["enabled"], -(r["mtime"] or 0)))
    return rows


def _row(sid: str, t: Transcript | None, entry: Entry | None, live: list[LiveProcess], status: Status | None) -> dict:
    open_elsewhere = [p for p in live if p.session_id == sid and p.managed_for != sid]
    # A session created here has no transcript until Claude Code writes one.
    pending = t is None and bool(entry and entry.cwd)
    cwd = t.cwd if t else (entry.cwd if pending else "")
    row = {
        "id": sid,
        "short_id": short_id(sid),
        "title": t.title if t else (entry.title if pending else "(transcript not found)"),
        "first_prompt": t.first_prompt if t else "",
        "cwd": cwd,
        "project": display_path(cwd),
        "mtime": t.mtime if t else (entry.updated_at if pending else None),
        "size": t.size if t else None,
        "remote_url": t.remote_url if t else "",
        "resume_cmd": resume_command(sid, cwd),
        "new": pending,
        "enabled": bool(entry and entry.enabled),
        "rc_name": entry.name if entry else "",
        "open_elsewhere": [{"pid": p.pid, "status": p.status} for p in open_elsewhere],
        "status": None,
    }
    if status:
        fields = asdict(status)
        fields["state"] = status.state.value
        if status.last_exit:
            # The screen is served separately; it's too big for every poll.
            fields["last_exit"] = {k: v for k, v in fields["last_exit"].items() if k != "screen"}
        row["status"] = fields
    return row
