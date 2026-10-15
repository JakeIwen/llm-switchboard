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

from .catalog import session_rows
from .conversation import recent_messages
from .live_registry import live_processes
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
_POST_ACTIONS = {"enable", "disable", "restart", "name", "rename", "delete", "restore", "move"}
_MAX_BODY = 16 * 1024
# Files under static/ the page may load; anything else under /static is a 404.
_STATIC_RE = re.compile(r"^/static/([a-z][a-z0-9-]*\.(js|css))$")
_STATIC_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}


class App:
    def __init__(self, store: Store, index: TranscriptIndex, supervisor: Supervisor, tmux: Tmux, trash: Trash,
                 registry=live_processes, actions: SessionActions | None = None):
        self.store = store
        self.index = index
        self.supervisor = supervisor
        self.tmux = tmux
        self.trash = trash
        self.registry = registry
        self.actions = actions or SessionActions(store, index, supervisor, tmux, trash, registry=registry)

    def list_sessions(self) -> dict:
        rows = session_rows(self.index.all(), self.store.load(), self.registry(), self.supervisor.snapshot())
        return {"host": socket.gethostname(), "now": time.time(), "sessions": rows}

    def list_trash(self) -> dict:
        items = [
            {"id": i.session_id, "short_id": short_id(i.session_id), "title": i.title,
             "project": i.project, "deleted_at": i.deleted_at}
            for i in self.trash.items()
        ]
        return {"trash": items}

    def messages(self, sid: str) -> dict:
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
        else:
            raise ValueError(f"unknown action {action}")
        self.supervisor.wake()
        return {"ok": True, "enabled": entry.enabled, "rc_name": entry.name}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "claude-always-remote"

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            static = _STATIC_RE.match(self.path)
            if static and (STATIC_DIR / static.group(1)).is_file():
                return self._send_file(STATIC_DIR / static.group(1), _STATIC_TYPES[static.group(2)])
            if self.path == "/api/sessions":
                return self._send_json(app.list_sessions())
            if self.path == "/api/trash":
                return self._send_json(app.list_trash())
            match = _ACTION_RE.match(self.path)
            if match and match.group(2) in _GET_ACTIONS and is_session_id(match.group(1)):
                sid, action = match.groups()
                try:
                    return self._send_json(app.screen(sid) if action == "screen" else app.messages(sid))
                except LookupError as e:
                    return self._send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if self.path == "/api/trash/empty":
                if not self._is_json():
                    return self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return self._send_json({"ok": True, "removed": app.trash.empty()})
            match = _ACTION_RE.match(self.path)
            if not match or match.group(2) not in _POST_ACTIONS:
                return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            sid, action = match.groups()
            if not is_session_id(sid):
                return self._send_json({"error": "bad session id"}, HTTPStatus.BAD_REQUEST)
            if not self._is_json():
                return self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            try:
                body = self._read_json()
                result = app.act(sid, action, body)
            except LookupError as e:
                return self._send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except Conflict as e:
                return self._send_json({"error": str(e)}, HTTPStatus.CONFLICT)
            except ValueError as e:
                return self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            log.info("%s %s from %s", action, sid[:8], self.client_address[0])
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


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
