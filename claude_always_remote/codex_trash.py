"""Codex sessions deleted from the picker.

Deleting one archives it in Codex (Codex's own soft delete), so it can be
restored by unarchiving. This file remembers which archived sessions were
deleted here, and when, so they appear under "Recently deleted" and are
deleted for good after the same 30 days as Claude Code sessions. Sessions
archived from Codex itself are left alone.
"""

from dataclasses import dataclass
from pathlib import Path
import json
import os
import tempfile
import threading

from .paths import CODEX_TRASH_FILE


@dataclass(frozen=True)
class CodexTrashed:
    session_id: str
    title: str
    project: str
    deleted_at: float


class CodexTrash:
    def __init__(self, path: Path = CODEX_TRASH_FILE):
        self.path = Path(path)
        self._lock = threading.Lock()

    def put(self, item: CodexTrashed) -> None:
        with self._lock:
            entries = self._load()
            entries[item.session_id] = {"title": item.title, "project": item.project, "deleted_at": item.deleted_at}
            self._write(entries)

    def remove(self, session_id: str) -> None:
        with self._lock:
            entries = self._load()
            if entries.pop(session_id, None) is not None:
                self._write(entries)

    def get(self, session_id: str) -> CodexTrashed | None:
        return next((i for i in self.items() if i.session_id == session_id), None)

    def items(self) -> list[CodexTrashed]:
        """Newest first."""
        items = [
            CodexTrashed(sid, str(e.get("title", "")), str(e.get("project", "")), float(e.get("deleted_at", 0)))
            for sid, e in self._load().items() if isinstance(e, dict)
        ]
        return sorted(items, key=lambda i: i.deleted_at, reverse=True)

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, entries: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".codex-trash-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(entries, f, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
