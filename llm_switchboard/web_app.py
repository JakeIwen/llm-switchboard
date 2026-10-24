"""The picker web app: a JSON API over the store and supervisor, plus one HTML page.

Every POST must be sent as application/json. Browsers can't send that
cross-origin without a CORS preflight, which this server never approves, so
another web page can't flip sessions on through a visitor's browser.
"""

from pathlib import Path
import socket
import time

from . import settings
from .catalog import codex_rows, session_rows, sort_rows
from .codex_actions import CodexActions
from .codex_client import CodexClient, CodexError, CodexUnavailable
from .codex_threads import CodexCatalog, codex_names, codex_short_id
from .codex_trash import CodexTrash
from .conversation import recent_messages
from .folders import new_session_folders
from .hosts import Hosts
from .live_registry import live_processes
from .names import Names, claude_names
from .paths import SEARCH_INDEX_FILE
from .search_api import SearchHandlers
from .session_actions import Conflict, SessionActions
from .session_id import is_session_id, short_id
from .store import Store
from .supervisor import Supervisor
from .tmux import Tmux
from .transcripts import TranscriptIndex
from .trash import Trash

class App:
    def __init__(self, store: Store, index: TranscriptIndex, supervisor: Supervisor, tmux: Tmux, trash: Trash,
                 registry=live_processes, actions: SessionActions | None = None, codex: CodexActions | None = None,
                 hosts: Hosts | None = None, search: SearchHandlers | None = None, settings_file: Path | None = None):
        self.store = store
        self.index = index
        self.supervisor = supervisor
        self.tmux = tmux
        self.trash = trash
        self.registry = registry
        self.actions = actions or SessionActions(store, index, supervisor, tmux, trash, registry=registry)
        # Codex sessions, when Codex support is on (codex_actions.py).
        self.codex = codex
        self.settings_file = settings_file
        self.search = search or SearchHandlers(SEARCH_INDEX_FILE, lambda: settings.load(self.settings_file))
        # Other machines' pushed sessions (hosts.py); None leaves them out.
        self.hosts = hosts

    def settings_payload(self, partial: dict | None = None) -> dict:
        current = (settings.load(self.settings_file) if partial is None
                   else settings.save(partial, self.settings_file))
        return {"settings": current, "defaults": settings.defaults()}

    def remote(self, current: settings.Settings | None = None) -> dict:
        if not self.hosts:
            return {"hosts": [], "sessions": [], "trash": [], "actions": []}
        return self.hosts.snapshot(current or settings.load(self.settings_file))

    def queue_remote(self, host: str, sid: str, action: str, body: dict) -> dict:
        if not self.hosts:
            raise LookupError("no other machines are connected")
        return self.hosts.queue(host, sid, action, body)

    def codex_threads(self) -> list:
        return self.codex.catalog.threads() if self.codex else []

    def list_sessions(self) -> dict:
        current = settings.load(self.settings_file)
        rows = session_rows(self.index.all(), self.store.load(), self.registry(), self.supervisor.snapshot(), current)
        codex = {"enabled": self.codex is not None, "error": ""}
        if self.codex:
            rows += codex_rows(self.codex.catalog.threads(), current)
            codex["error"] = self.codex.catalog.error
        remote = self.remote(current)
        rows += remote["sessions"]
        return {"host": socket.gethostname(), "now": time.time(), "sessions": sort_rows(rows),
                "notifications": self.supervisor.notifier.enabled, "codex": codex,
                "hosts": remote["hosts"], "host_actions": remote["actions"], "settings": current}

    def list_folders(self) -> dict:
        return {"folders": [
            {"cwd": f.cwd, "project": f.project, "sessions": f.sessions, "blocked": f.blocked,
             "codex_blocked": f.codex_blocked if self.codex else "Codex support is off."}
            for f in new_session_folders(self.index.all(), codex=self.codex_threads())
        ]}

    def list_trash(self) -> dict:
        items = [
            {"id": i.session_id, "tool": "claude", "short_id": short_id(i.session_id), "title": i.title,
             "project": i.project, "deleted_at": i.deleted_at}
            for i in self.trash.items()
        ]
        if self.codex:
            items += [
                {"id": i.session_id, "tool": "codex", "short_id": codex_short_id(i.session_id), "title": i.title,
                 "project": i.project, "deleted_at": i.deleted_at}
                for i in self.codex.trash_items()
            ]
        items += self.remote()["trash"]
        return {"trash": sorted(items, key=lambda i: i["deleted_at"], reverse=True)}

    def empty_trash(self) -> dict:
        removed = self.trash.empty() + (self.codex.empty_trash() if self.codex else 0)
        return {"ok": True, "removed": removed}

    def create(self, body: dict) -> dict:
        if body.get("tool") == "codex":
            if not self.codex:
                raise ValueError("Codex support is off")
            return self.codex.create(body.get("cwd"), body.get("name"), body.get("prompt"))
        return self.actions.create(body.get("cwd"), body.get("name"))

    def messages(self, sid: str) -> dict:
        if self.codex and self.codex.owns(sid):
            return {"messages": self.codex.catalog.messages(sid)}
        transcript = self.index.get(sid)
        if transcript is None:
            raise LookupError("no transcript with that session id")
        return {"messages": recent_messages(transcript.path)}

    def screen(self, sid: str) -> dict:
        status = self.supervisor.snapshot().get(sid)
        if status and status.window_id:
            text = self.tmux.capture(status.window_id, history=200)
            if text:
                return {"source": "live", "screen": text}
        if status and status.last_exit:
            return {"source": "last-exit", "screen": status.last_exit.screen, "exit": status.last_exit.status}
        return {"source": "none", "screen": ""}

    def act(self, sid: str, action: str, body: dict) -> dict:
        if self.codex and self.codex.owns(sid):
            return self.codex.act(sid, action, body)
        if action == "enable":
            if self.index.get(sid) is None:
                raise LookupError("no transcript with that session id")
            entry = self.store.set_enabled(sid, True)
        elif action == "disable":
            entry = self.store.set_enabled(sid, False)
        elif action == "name":
            name = body.get("name", "")
            if not isinstance(name, str) or len(name) > 120:
                raise ValueError("name must be a string of at most 120 characters")
            entry = self.store.set_name(sid, name)
        elif action == "restart":
            self.supervisor.request_restart(sid)
            return {"ok": True}
        elif action == "rename":
            return self.actions.rename(sid, body.get("title"))
        elif action == "delete":
            return self.actions.delete(sid)
        elif action == "restore":
            return self.actions.restore(sid)
        elif action == "move":
            return self.actions.move(sid, body.get("cwd"))
        elif action == "fork":
            return self.actions.fork(sid, body.get("cwd"), body.get("title"), bool(body.get("keep")))
        else:
            raise ValueError(f"unknown action {action}")
        self.supervisor.wake()
        return {"ok": True, "enabled": entry.enabled, "rc_name": entry.name}


def make_app(store: Store, index: TranscriptIndex, supervisor: Supervisor, tmux: Tmux, trash: Trash,
             codex_socket: Path | None, registry=live_processes, codex_trash: CodexTrash | None = None,
             folders=new_session_folders, hosts: Hosts | None = None, settings_file: Path | None = None) -> App:
    """The App with both session sources wired up, sharing one name check.
    codex_socket None leaves Codex out; hosts None leaves other machines out."""
    names = Names(claude_names(index, store))
    codex = None
    if codex_socket is not None:
        catalog = CodexCatalog(CodexClient(codex_socket))
        names.add_source(codex_names(catalog))
        codex = CodexActions(catalog.client, catalog, codex_trash or CodexTrash(), names)
    threads = (lambda: codex.catalog.threads()) if codex else (lambda: [])
    actions = SessionActions(store, index, supervisor, tmux, trash, registry=registry, names=names,
                             folders=lambda transcripts: folders(transcripts, codex=threads()))
    return App(store, index, supervisor, tmux, trash, registry=registry, actions=actions, codex=codex, hosts=hosts,
               settings_file=settings_file)


