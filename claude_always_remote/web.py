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
from .live_registry import live_processes
from .session_id import is_session_id
from .store import Store
from .supervisor import Supervisor
from .tmux import Tmux
from .transcripts import TranscriptIndex

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_ACTION_RE = re.compile(r"^/api/sessions/([^/]+)/(enable|disable|restart|name|screen)$")
_MAX_BODY = 16 * 1024


class App:
    def __init__(self, store: Store, index: TranscriptIndex, supervisor: Supervisor, tmux: Tmux, registry=live_processes):
        self.store = store
        self.index = index
        self.supervisor = supervisor
        self.tmux = tmux
        self.registry = registry

    def list_sessions(self) -> dict:
        rows = session_rows(self.index.all(), self.store.load(), self.registry(), self.supervisor.snapshot())
        return {"host": socket.gethostname(), "now": time.time(), "sessions": rows}

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
            if self.path == "/api/sessions":
                return self._send_json(app.list_sessions())
            match = _ACTION_RE.match(self.path)
            if match and match.group(2) == "screen" and is_session_id(match.group(1)):
                return self._send_json(app.screen(match.group(1)))
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            match = _ACTION_RE.match(self.path)
            if not match or match.group(2) == "screen":
                return self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            sid, action = match.groups()
            if not is_session_id(sid):
                return self._send_json({"error": "bad session id"}, HTTPStatus.BAD_REQUEST)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            try:
                body = self._read_json()
                result = app.act(sid, action, body)
            except LookupError as e:
                return self._send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except ValueError as e:
                return self._send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            log.info("%s %s from %s", action, sid[:8], self.client_address[0])
            self._send_json(result)

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
