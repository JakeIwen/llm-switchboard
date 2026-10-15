"""Deleted transcripts, kept for a while so a delete can be undone.

A deleted session's <id>.jsonl, and the <id>/ folder Claude Code keeps beside
it for subagent and tool output, move to TRASH_DIR/<id>/ with a note of where
they came from. Entries older than RETENTION_S are removed for good.
"""

from dataclasses import dataclass
from pathlib import Path
import json
import shutil
import time

from .paths import TRASH_DIR
from .session_id import is_session_id

RETENTION_S = 30 * 86400
_META = "meta.json"


@dataclass(frozen=True)
class Trashed:
    session_id: str
    title: str
    project: str
    original: Path
    deleted_at: float


class Trash:
    def __init__(self, root: Path = TRASH_DIR, clock=time.time):
        self.root = Path(root)
        self.clock = clock

    def put(self, session_id: str, transcript: Path, title: str, project: str) -> Trashed:
        entry = self._entry(session_id)
        if entry.exists():
            shutil.rmtree(entry)
        entry.mkdir(parents=True)
        companion = transcript.with_suffix("")
        item = Trashed(session_id, title, project, transcript, self.clock())
        (entry / _META).write_text(json.dumps({
            "title": title, "project": project, "original": str(transcript), "deleted_at": item.deleted_at,
        }))
        shutil.move(str(transcript), entry / transcript.name)
        if companion.is_dir():
            shutil.move(str(companion), entry / companion.name)
        return item

    def restore(self, session_id: str) -> Path:
        item = self.get(session_id)
        if item is None:
            raise LookupError("not in the trash")
        if item.original.exists():
            raise FileExistsError(f"{item.original} already exists")
        entry = self._entry(session_id)
        item.original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry / item.original.name), item.original)
        companion = entry / session_id
        if companion.is_dir() and not item.original.with_suffix("").exists():
            shutil.move(str(companion), item.original.with_suffix(""))
        shutil.rmtree(entry)
        return item.original

    def get(self, session_id: str) -> Trashed | None:
        try:
            meta = json.loads((self._entry(session_id) / _META).read_text())
        except (OSError, ValueError):
            return None
        return Trashed(
            session_id=session_id,
            title=str(meta.get("title", "")),
            project=str(meta.get("project", "")),
            original=Path(meta["original"]),
            deleted_at=float(meta.get("deleted_at", 0)),
        )

    def items(self) -> list[Trashed]:
        """Everything in the trash, newest first, after dropping expired entries."""
        if not self.root.is_dir():
            return []
        found = []
        for entry in self.root.iterdir():
            item = self.get(entry.name) if is_session_id(entry.name) else None
            if item and self.clock() - item.deleted_at > RETENTION_S:
                shutil.rmtree(entry, ignore_errors=True)
            elif item:
                found.append(item)
        return sorted(found, key=lambda i: i.deleted_at, reverse=True)

    def empty(self) -> int:
        """Remove everything in the trash for good; returns how many sessions."""
        items = self.items()
        for item in items:
            shutil.rmtree(self._entry(item.session_id), ignore_errors=True)
        return len(items)

    def _entry(self, session_id: str) -> Path:
        if not is_session_id(session_id):
            raise ValueError(f"not a session id: {session_id!r}")
        return self.root / session_id
