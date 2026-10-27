"""Read-only Mac transcript search for an allow-listed page in its own browser."""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path

from .host_protocol import SEARCH_PORT, host_name
from .paths import SEARCH_INDEX_FILE
from .search_api import SearchHandlers, route_search_get

log = logging.getLogger(__name__)


class MacSearchServer(ThreadingHTTPServer):
    def __init__(self, host: str, *, port: int = SEARCH_PORT,
                 index_file: Path = SEARCH_INDEX_FILE, origins=()):
        self.host_name = host_name(host)
        self.origins = frozenset(origins)
        self.search = SearchHandlers(index_file)
        # A busy fixed port is an error, not permission to expose a different endpoint.
        super().__init__(("127.0.0.1", port), MacSearchRequest)
        if not self.origins:
            log.warning("Mac search has no page origins configured; cross-origin reads are disabled")


class MacSearchRequest(BaseHTTPRequestHandler):
    server: MacSearchServer

    def log_message(self, format, *args) -> None:
        # The default access/error logger includes the request's private query text.
        pass

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        port = self.server.server_port
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self._json({"error": "invalid Host header"}, HTTPStatus.FORBIDDEN)
            return False
        if self.command not in {"GET", "OPTIONS"}:
            self._json({"error": "method not allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)
            return False
        return True

    def _origin(self) -> str | None:
        origins = self.headers.get_all("Origin", [])
        return origins[0] if len(origins) == 1 and origins[0] in self.server.origins else None

    def _headers(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        # Vary also on denied origins so a cache cannot reuse a different origin's response.
        self.send_header("Vary", "Origin")
        origin = self._origin()
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)

    def _json(self, payload: dict, status: HTTPStatus) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status)
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            self.send_header("Allow", "GET, OPTIONS")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json({"ok": True}, HTTPStatus.OK)
            return
        routed = route_search_get(self.path, self.server.search, lambda: ())
        if routed is None:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        payload, status = routed
        for result in payload.get("results", []):
            result["host"] = self.server.host_name
        if payload.get("detail"):
            payload["detail"]["result"]["host"] = self.server.host_name
        self._json(payload, status)

    def do_OPTIONS(self) -> None:
        if self._origin() is None:
            self._json({"error": "origin not allowed"}, HTTPStatus.FORBIDDEN)
            return
        self._headers(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()


def serve(host: str, *, port: int = SEARCH_PORT, origins=()) -> None:
    with MacSearchServer(host, port=port, origins=origins) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
