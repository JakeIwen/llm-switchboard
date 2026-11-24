"""The desk's HTTPS listener (docs/voice-desk.md, "HTTPS API").

Standard library only: `ssl` (TLS 1.2+, the leaf from `DeskConfig.tls_cert`
and `tls_key`) around `http.server.ThreadingHTTPServer`, bound to 127.0.0.1.
There is no plain-HTTP mode: a missing cert or a key readable by anyone but
its owner stops the hub from starting.

Every request needs the exact Host; POSTs also need the exact Origin, a JSON
content type and a body of at most 16 KiB; `/desk/api/*` needs the bearer
token, which the hub checks again together with the mutation, after the
body is read. No CORS header is ever sent. The handshake runs in the
connection's own thread, so a slow client can't stall the accept loop, and
concurrent connections are capped.
"""

from dataclasses import replace
import contextlib
import json
import logging
import os
from pathlib import Path
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .config import DeskConfig
from .model import (
    HTTP_REQUEST_TIMEOUT_SECONDS, LONG_POLL_SECONDS, MAX_BODY_BYTES, MAX_HTTP_CONNECTIONS, DeskError,
)
from .page_build import PageFiles
from .refusals import Refusal
from .shallow_json import loads_shallow

log = logging.getLogger("llm_switchboard.desk")

DESK_DIR = Path(__file__).resolve().parent
STATIC_DIR = DESK_DIR / "static"
BIND_HOST = "127.0.0.1"
# Request bodies are flat objects.
MAX_BODY_DEPTH = 4

_HTML = "text/html; charset=utf-8"
_JS = "text/javascript; charset=utf-8"
_CSS = "text/css; charset=utf-8"
_JSON = "application/json"

# The only files the page may load: URL path -> (file, content type).
STATIC_ROUTES = {
    "/desk": (STATIC_DIR / "index.html", _HTML),
    "/desk/app.js": (STATIC_DIR / "app.js", _JS),
    "/desk/app.css": (STATIC_DIR / "app.css", _CSS),
    "/desk/text_rule.js": (STATIC_DIR / "text_rule.js", _JS),
    "/desk/desk_logic.js": (STATIC_DIR / "desk_logic.js", _JS),
    "/desk/desk_hosts.js": (STATIC_DIR / "desk_hosts.js", _JS),
    "/desk/conversation_vocabulary.js": (STATIC_DIR / "conversation_vocabulary.js", _JS),
    "/desk/conversation_logic.js": (STATIC_DIR / "conversation_logic.js", _JS),
    "/desk/sound_tracker.js": (STATIC_DIR / "sound_tracker.js", _JS),
    "/desk/conversation_answers.js": (STATIC_DIR / "conversation_answers.js", _JS),
    "/desk/conversation_timers.js": (STATIC_DIR / "conversation_timers.js", _JS),
    "/desk/conversation_fetches.js": (STATIC_DIR / "conversation_fetches.js", _JS),
    "/desk/conversation_recognition.js": (STATIC_DIR / "conversation_recognition.js", _JS),
    "/desk/conversation.js": (STATIC_DIR / "conversation.js", _JS),
    "/desk/conversation_view.js": (STATIC_DIR / "conversation_view.js", _JS),
    "/desk/page_audio.js": (STATIC_DIR / "page_audio.js", _JS),
    "/desk/startup.js": (STATIC_DIR / "startup.js", _JS),
    "/desk/link_view.js": (STATIC_DIR / "link_view.js", _JS),
    "/desk/hub_api.js": (STATIC_DIR / "hub_api.js", _JS),
    # The hub's own cases, so the page's mirror of the rule checks the same table.
    "/desk/text_rule_cases.json": (DESK_DIR / "text_rule_cases.json", _JSON),
}

# POST path -> (hub method, needs the bearer token).
POST_ROUTES = {
    "/desk/pair/start": ("pair_start", False),
    "/desk/pair/claim": ("pair_claim", False),
    "/desk/api/ticket": ("ticket", True),
    "/desk/api/send": ("send", True),
    "/desk/api/clear": ("clear", True),
    "/desk/api/enroll": ("enroll", True),
    "/desk/api/unenroll": ("unenroll", True),
    "/desk/api/enroll-host": ("enroll_host", True),
    "/desk/api/unenroll-host": ("unenroll_host", True),
    "/desk/api/startup/ack": ("startup_ack", True),
    "/desk/api/conversation": ("conversation", True),
    "/desk/api/conversation/beat": ("conversation_beat", True),
    "/desk/api/conversation/end": ("conversation_end", True),
}
STATE_PATH = "/desk/api/state"
STARTUP_PATH = "/desk/api/startup"
# A refused body up to this size is read and dropped so the error reaches the client.
_MAX_DRAIN_BYTES = 4 * MAX_BODY_BYTES

SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    # Not no-referrer: under it a browser sends `Origin: null` on the page's
    # own POSTs (Fetch, "serialize a request origin"), which the exact Origin
    # guard refuses. same-origin keeps the Origin and sends no referrer
    # anywhere else.
    ("Referrer-Policy", "same-origin"),
    ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
)


class TlsSetupError(Exception):
    """The leaf certificate or key is missing or unsafe; the hub won't start."""


def make_tls_context(config: DeskConfig) -> ssl.SSLContext:
    for path in (config.tls_cert, config.tls_key):
        if not path.is_file():
            raise TlsSetupError(f"missing {path} (run scripts/desk-ca.sh)")
    if os.stat(config.tls_key).st_mode & 0o077:
        raise TlsSetupError(f"{config.tls_key} must be 0600")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config.tls_cert, config.tls_key)
    return context


class _TlsThreadingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, tls: ssl.SSLContext, max_connections: int):
        self._tls = tls
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def finish_request(self, request, client_address):
        request.settimeout(HTTP_REQUEST_TIMEOUT_SECONDS)
        try:
            tls = self._tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            with contextlib.suppress(OSError):
                tls.close()


class DeskHttpServer:
    """`api` is the hub: authenticate, state_view, wait_for_change, startup_view and the
    POST_ROUTES methods (each takes the parsed body and may raise DeskError)."""

    def __init__(self, config: DeskConfig, api, max_connections: int = MAX_HTTP_CONNECTIONS,
                 long_poll_seconds: float = LONG_POLL_SECONDS):
        self.config = config
        self.api = api
        self.max_connections = max_connections
        self.long_poll_seconds = long_poll_seconds
        # The page as this hub serves it for its whole run, and its build id.
        self.page = PageFiles(STATIC_ROUTES)
        self._server: _TlsThreadingServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.config.port

    @property
    def page_build(self) -> str:
        return self.page.build

    def start(self) -> None:
        tls = make_tls_context(self.config)
        server = _TlsThreadingServer((BIND_HOST, self.config.port), _make_handler(self), tls, self.max_connections)
        # Port 0 (tests) binds a free port; the Host and Origin checks use the real one.
        self.config = replace(self.config, port=server.server_address[1])
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="desk-https", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


def _make_handler(server: DeskHttpServer):
    class Handler(_DeskHandler):
        desk = server
    return Handler


class _DeskHandler(BaseHTTPRequestHandler):
    desk: DeskHttpServer
    timeout = HTTP_REQUEST_TIMEOUT_SECONDS
    # One request per connection: a kept-alive idle socket would hold a slot.
    protocol_version = "HTTP/1.0"

    def version_string(self) -> str:
        return "desk"

    def log_message(self, format, *args) -> None:
        log.debug("desk http: " + format, *args)

    def do_GET(self) -> None:
        self._dispatch(self._get)

    def do_POST(self) -> None:
        self._dispatch(self._post)

    def _dispatch(self, route) -> None:
        self._body_read = False
        try:
            self._check_single("Host", self.desk.config.host_header, Refusal.WRONG_HOST)
            route(self.path.partition("?")[0])
        except DeskError as exc:
            self._drain_unread_body()
            self._json(exc.status, {"error": exc.reason, **exc.fields},
                       [("WWW-Authenticate", "Bearer")] if exc.status == 401 else [])
        except Exception:
            log.exception("desk http: %s %s failed", self.command, self.path.partition("?")[0][:64])
            self._json(500, {"error": Refusal.INTERNAL_ERROR})

    def _check_single(self, name: str, expected: str, refusal: Refusal) -> None:
        values = self.headers.get_all(name) or []
        if len(values) != 1 or values[0] != expected:
            raise DeskError(403, refusal)

    def _token(self) -> str:
        values = self.headers.get_all("Authorization") or []
        if len(values) != 1 or not values[0].startswith("Bearer "):
            raise DeskError(401, Refusal.UNAUTHORIZED)
        token = values[0][len("Bearer "):]
        if not self.desk.api.authenticate(token):
            raise DeskError(401, Refusal.UNAUTHORIZED)
        return token

    def _get(self, path: str) -> None:
        if path in STATIC_ROUTES:
            served = self.desk.page.get(path)
            if served is None:
                raise DeskError(404, Refusal.NOT_FOUND)
            self._respond(200, *served)
        elif path == STATE_PATH:
            self._state()
        elif path == STARTUP_PATH:
            token = self._token()
            self._json(200, self.desk.api.startup_view(token))
        else:
            raise DeskError(404, Refusal.NOT_FOUND)

    def _state(self) -> None:
        token = self._token()
        query = parse_qs(self.path.partition("?")[2], keep_blank_values=True)
        after = query.get("after")
        if after is not None:
            if len(after) != 1 or not after[0].isdigit() or len(after[0]) > 18:
                raise DeskError(400, Refusal.BAD_AFTER)
            if not self.desk.api.wait_for_change(int(after[0]), token, self.desk.long_poll_seconds):
                raise DeskError(401, Refusal.UNAUTHORIZED)
        self._json(200, self.desk.api.state_view())

    def _post(self, path: str) -> None:
        if path not in POST_ROUTES:
            raise DeskError(404, Refusal.NOT_FOUND)
        method, protected = POST_ROUTES[path]
        self._check_single("Origin", self.desk.config.origin, Refusal.WRONG_ORIGIN)
        if len(self.headers.get_all("Content-Type") or []) != 1 or \
                self.headers.get_content_type() != "application/json":
            raise DeskError(400, Refusal.NOT_JSON)
        if self.headers.get("Transfer-Encoding") is not None:
            raise DeskError(400, Refusal.LENGTH_REQUIRED)
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise DeskError(400, Refusal.LENGTH_REQUIRED)
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise DeskError(413, Refusal.BODY_TOO_LARGE)
        token = self._token() if protected else None
        raw = self.rfile.read(length)
        self._body_read = True
        if len(raw) != length:
            raise DeskError(400, Refusal.TRUNCATED_BODY)
        try:
            body = loads_shallow(raw, MAX_BODY_DEPTH)
        except ValueError:
            raise DeskError(400, Refusal.BAD_JSON) from None
        if not isinstance(body, dict):
            raise DeskError(400, Refusal.BAD_JSON)
        handler = getattr(self.desk.api, method)
        self._json(200, handler(body, token) if protected else handler(body))

    def _drain_unread_body(self) -> None:
        """Read (and drop) a refused POST's body before answering: closing
        with unread bytes makes the kernel reset the connection, and the
        client would lose the error response. Bounded by size and timeout."""
        if self.command != "POST" or self._body_read:
            return
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) == 1 and lengths[0].isdigit() and int(lengths[0]) <= _MAX_DRAIN_BYTES:
            with contextlib.suppress(OSError):
                self.rfile.read(int(lengths[0]))
        self._body_read = True

    def send_error(self, code, message=None, explain=None) -> None:
        """Parse errors (bad request line, oversize headers, unknown method)
        get the same JSON shape and headers as everything else."""
        self.close_connection = True
        self._json(code if code in (400, 404, 413, 414, 431, 501, 505) else 400,
                   {"error": Refusal.METHOD_NOT_ALLOWED if code == 501 else Refusal.BAD_REQUEST})

    def _json(self, status: int, body: dict, extra=()) -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        self._respond(status, data, _JSON, extra)

    def _respond(self, status: int, data: bytes, content_type: str, extra=()) -> None:
        with contextlib.suppress(OSError):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (*SECURITY_HEADERS, *extra):
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
