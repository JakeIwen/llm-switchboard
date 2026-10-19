"""Mac-local allow-listed edits. No supervisor, tmux, launch or prompt path."""

from pathlib import Path
import uuid

from .codex_actions import CodexActions
from .codex_client import CodexError, CodexUnavailable
from .codex_threads import CodexState, codex_names
from .codex_trash import CodexTrash
from .conflict import Conflict
from .folders import existing_folder
from .host_actions_protocol import ActionKind, action
from .host_protocol import SessionState
from .mac_catalog import MacCatalog
from .mac_uploads import MacUploads
from .names import Names
from .relocate import fork, relocate
from .transcripts import custom_title_record, summarize
from .trash import Trash


class MacActions:
    def __init__(self, catalog: MacCatalog, trash=None, codex_trash=None, uploads=None):
        self.catalog, self.index = catalog, catalog.index
        self.trash = trash or Trash()
        self.uploads = uploads or MacUploads()
        self.names = Names(lambda: ((t.session_id, t.cwd, t.title) for t in self.index.all()), codex_names(catalog.codex))
        self.codex = CodexActions(catalog.codex.client, catalog.codex, codex_trash or CodexTrash(), self.names)

    def execute(self, raw: dict) -> tuple[dict, dict | None]:
        item = action(raw)
        sid, kind, body = item["session_id"], item["action"], item["body"]
        if any(p.session_id == sid for p in self.catalog.registry()):
            raise Conflict("Open in a Claude process on the Mac; close it before changing or exporting it.")
        if item["tool"] == "codex":
            return self._codex(sid, kind, body), None
        if kind == ActionKind.RESTORE:
            return self._restore(sid), None
        transcript = self.index.get(sid)
        if transcript is None:
            raise LookupError("no Claude transcript with that id on the Mac")
        if kind == ActionKind.FORK_PI:
            if transcript.cwd != item["source_cwd"]:
                raise Conflict("Mac folder changed since this action was queued; refresh and try again.")
            return {"ok": True}, self.uploads.export(transcript, item["id"])
        if kind == ActionKind.RENAME:
            self.names.refuse_duplicate(transcript.cwd, body["title"], sid)
            with open(transcript.path, "ab") as f:
                f.write(custom_title_record(sid, body["title"]))
        elif kind == ActionKind.DELETE:
            self.trash.put(sid, transcript.path, transcript.title, transcript.cwd)
        elif kind in (ActionKind.MOVE, ActionKind.FORK):
            cwd = existing_folder(body["cwd"])
            title = body.get("title", transcript.title)
            self.names.refuse_duplicate(cwd, title, sid if kind == ActionKind.MOVE else None)
            if kind == ActionKind.MOVE:
                if cwd != transcript.cwd:
                    relocate(transcript.path, sid, transcript.cwd, cwd, self.index.projects_dir)
            else:
                new = str(uuid.uuid4())
                fork(transcript.path, sid, new, transcript.cwd, cwd, title, self.index.projects_dir)
                return {"ok": True, "id": new}, None
        return {"ok": True}, None

    def _codex(self, sid, kind, body) -> dict:
        self.catalog.codex.threads()
        if self.catalog.codex.error or not self.catalog.codex.client.enabled:
            raise Conflict("Mac Codex is list-only: its app-server daemon is unavailable.")
        thread = self.catalog.codex.get(sid)
        if thread and thread.state == CodexState.ACTIVE:
            raise Conflict("Codex is working on a turn on the Mac; wait until it finishes.")
        if kind == ActionKind.RESTORE:
            item = self.codex.trash.get(sid)
            if item:
                self.names.refuse_duplicate(existing_folder(item.project), item.title, sid)
        return self.codex.act(sid, kind, body)

    def _restore(self, sid) -> dict:
        item = self.trash.get(sid)
        if item is None:
            raise LookupError("not in the Mac trash")
        transcript = summarize(self.trash.root / sid / f"{sid}.jsonl", metadata_only=True)
        if transcript is None:
            raise ValueError("deleted transcript has no metadata")
        cwd = existing_folder(transcript.cwd)
        self.names.refuse_duplicate(cwd, item.title, sid)
        self.trash.restore(sid)
        return {"ok": True}

    def trash_rows(self) -> list[dict]:
        rows = []
        for item in self.trash.items():
            rows.append(dict(id=item.session_id, tool="claude", title=item.title, cwd=item.project,
                             mtime=None, size=None, state=SessionState.CLOSED, mutable=True, deleted_at=item.deleted_at))
        try:
            codex_items = self.codex.trash.items() if self.catalog.codex.error else self.codex.trash_items()
        except (CodexUnavailable, CodexError):
            codex_items = self.codex.trash.items()
        for item in codex_items:
            cwd = str(Path(item.project).expanduser())
            rows.append(dict(id=item.session_id, tool="codex", title=item.title, cwd=cwd,
                             mtime=None, size=None, state=SessionState.CLOSED, mutable=not bool(self.catalog.codex.error),
                             deleted_at=item.deleted_at))
        return rows
