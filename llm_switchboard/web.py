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


# Permanent public imports for CLI and existing callers.
from .web_app import App, make_app


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
