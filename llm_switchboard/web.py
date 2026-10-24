"""The picker web app: a JSON API over the store and supervisor, plus one HTML page.

Every POST must be sent as application/json. Browsers can't send that
cross-origin without a CORS preflight, which this server never approves, so
another web page can't flip sessions on through a visitor's browser.
"""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import logging
import re
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
from .host_web import action_route
from .listen import allowed_peer
from .live_registry import live_processes
from .names import Names, claude_names
from .paths import SEARCH_INDEX_FILE
from .search_api import SearchHandlers, route_search_get
from .session_actions import Conflict, SessionActions
from .session_id import is_session_id, short_id
from .store import Store
from .supervisor import Supervisor
from .tmux import Tmux
from .transcripts import TranscriptIndex
from .trash import Trash

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_ACTION_RE = re.compile(r"^/api/sessions/([^/]+)/([a-z]+)$")
_GET_ACTIONS = {"screen", "messages"}
_POST_ACTIONS = {"enable", "disable", "restart", "name", "rename", "delete", "restore", "move", "fork"}
_MAX_BODY = 16 * 1024
# Files under static/ the page may load; anything else under /static is a 404.
_STATIC_RE = re.compile(r"^/static/([a-z][a-z0-9-]*\.(js|css))$")
_STATIC_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}


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


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "llm-switchboard"

        def do_GET(self):
            search = route_search_get(self.path, app.search, lambda: app.list_sessions()["sessions"])
            if search is not None:
                return self._send_json(*search)
            if self.path in ("/", "/index.html"):
                return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            static = _STATIC_RE.match(self.path)
            if static and (STATIC_DIR / static.group(1)).is_file():
                return self._send_file(STATIC_DIR / static.group(1), _STATIC_TYPES[static.group(2)])
            if self.path == "/api/settings":
                return self._send_json(app.settings_payload())
            if self.path == "/api/sessions":
                return self._send_json(app.list_sessions())
            if self.path == "/api/trash":
                return self._send_json(app.list_trash())
            if self.path == "/api/folders":
                return self._send_json(app.list_folders())
            match = _ACTION_RE.match(self.path)
            if match and match.group(2) in _GET_ACTIONS and is_session_id(match.group(1)):
                sid, action = match.groups()
                try:
                    return self._send_json(app.screen(sid) if action == "screen" else app.messages(sid))
                except LookupError as e:
                    return self._send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
                except (CodexUnavailable, CodexError) as e:
                    return self._send_json({"error": str(e)}, HTTPStatus.SERVICE_UNAVAILABLE)
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if self.path == "/api/settings":
                if not self._is_json():
                    return self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return self._guarded(app.settings_payload, "settings")
            remote = action_route(self.path)
            if remote:
                if not self._is_json():
                    return self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                host, sid, action = remote
                return self._guarded(lambda body: app.queue_remote(host, sid, action, body), f"{host} {action}")
            if self.path in ("/api/trash/empty", "/api/sessions/new"):
                if not self._is_json():
                    return self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                if self.path == "/api/trash/empty":
                    return self._guarded(lambda body: app.empty_trash(), "empty trash")
                return self._guarded(app.create, "new")
            match = _ACTION_RE.match(self.path)
            if not match or match.group(2) not in _POST_ACTIONS:
                return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            sid, action = match.groups()
            if not is_session_id(sid):
                return self._send_json({"error": "bad session id"}, HTTPStatus.BAD_REQUEST)
            if not self._is_json():
                return self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            self._guarded(lambda body: app.act(sid, action, body), f"{action} {sid[:8]}")

        def _guarded(self, run, what: str):
            """Run an action on the JSON body, mapping its errors to HTTP statuses."""
            try:
                result = run(self._read_json())
            except LookupError as e:
                return self._send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except CodexUnavailable as e:
                return self._send_json({"error": str(e)}, HTTPStatus.SERVICE_UNAVAILABLE)
            except Conflict as e:
                return self._send_json({"error": str(e)}, HTTPStatus.CONFLICT)
            except ValueError as e:
                return self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            log.info("%s from %s", what, self.client_address[0])
            self._send_json(result)

        def _is_json(self) -> bool:
            return self.headers.get("Content-Type", "").split(";")[0].strip() == "application/json"

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            return body

        def _send_json(self, payload, status=HTTPStatus.OK):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path: Path, content_type: str):
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.client_address[0], fmt % args)

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def verify_request(self, request, client_address) -> bool:
        return allowed_peer(client_address[0])


class _Server6(_Server):
    address_family = socket.AF_INET6


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    """Serve `app` on one address, answering only localhost and Tailscale peers."""
    return (_Server6 if ":" in host else _Server)((host, port), make_handler(app))
