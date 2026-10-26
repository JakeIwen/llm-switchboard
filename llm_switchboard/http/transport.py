"""HTTP compatibility: legacy JSON bytes, validation errors and POST guards."""

import json
from functools import cache
import logging

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import TypeAdapter, ValidationError

from ..host_web import action_route
from ..page import PREFIX as PREVIEW_PREFIX

from ..codex_client import CodexError, CodexUnavailable
from ..conflict import Conflict
from .responses import ErrorResponse
from ..session_id import is_session_id

MAX_BODY = 16 * 1024
log = logging.getLogger(__name__)


@cache
def _adapter(model):
    return TypeAdapter(model)


def json_response(payload, model, status=200, *, request: Request | None = None) -> Response:
    # Validate without reserializing the model: absent keys, integer vs float,
    # insertion order, escaping and separators are all part of the old wire format.
    try:
        _adapter(model).validate_python(payload)
    except ValidationError as exc:
        route = f"{request.method} {request.url.path}" if request else "error response"
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        log.error("Response validation failed for %s (%d errors): %s", route, exc.error_count(), errors[:5])
        if request and request.app.state.strict_responses:
            raise
        # Schema drift must not hide otherwise usable sessions on a production poll.
    return Response(json.dumps(payload).encode(), status_code=status,
                    headers={"Content-Type": "application/json", "Cache-Control": "no-store"})


def error_response(message, status=400) -> Response:
    return json_response({"error": str(message)}, ErrorResponse, status)


def validation_error(request: Request, exc: RequestValidationError) -> Response:
    first = exc.errors()[0]
    return error_response(first.get("ctx", {}).get("error", first["msg"]))


def unexpected_error(request: Request, exc: Exception) -> Response:
    log.error("Unhandled exception for %s %s", request.method, request.url.path,
              exc_info=(type(exc), exc, exc.__traceback__))
    return error_response("internal server error", 500)


def audit_post(request: Request) -> None:
    remote = action_route(request.url.path)
    if remote:
        host, _, action = remote
        what = f"{host} {action}"
    elif "sid" in request.path_params:
        what = f"{request.url.path.rsplit('/', 1)[-1]} {request.path_params['sid'][:8]}"
    else:
        what = {"/api/settings": "settings", "/api/sessions/new": "new",
                "/api/trash/empty": "empty trash"}.get(request.url.path, request.url.path)
    log.info("%s from %s", what, request.client[0] if request.client else "unknown peer")


class RawPathMiddleware:
    """The legacy router matches escaped request targets, not decoded paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["raw_path"].decode("latin-1")
            if path.startswith("//"):
                path = "/" + path.lstrip("/")
            scope = {**scope, "path": path, "raw_path": path.encode("latin-1")}
        await self.app(scope, receive, send)


class CompatibilityRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def compatible(request: Request):
            rejected = self._path_error(request)
            if rejected is not None:
                return rejected
            try:
                if request.method == "POST":
                    rejected = await self._body(request)
                    if rejected is not None:
                        return rejected
                response = await handler(request)
                if request.method == "POST" and 200 <= response.status_code < 300:
                    audit_post(request)
                return response
            except LookupError as exc:
                if {"sid", "host"} & request.path_params.keys():
                    return error_response(exc, 404)
                raise
            except CodexUnavailable as exc:
                return error_response(exc, 503)
            except Conflict as exc:
                return error_response(exc, 409)
            except CodexError as exc:
                if request.method == "GET":
                    return error_response(exc, 503)
                raise
            except ValidationError:
                raise  # Invalid server output is a bug, not a bad client request.
            except ValueError as exc:
                if request.method == "POST":
                    return error_response(exc)
                raise
        return compatible

    def _path_error(self, request):
        path = request.scope.get("raw_path", b"").decode("ascii")
        if request.method == "GET" and (path == PREVIEW_PREFIX.rstrip("/") or path.startswith(PREVIEW_PREFIX)):
            return None  # The preview resolver owns its path and query rules.
        if path != request.url.path:
            return error_response("not found", 404)
        if request.url.query and not path.startswith("/api/search"):
            return error_response("not found", 404)
        if path.startswith("/api/hosts/") and action_route(path) is None:
            return error_response("not found", 404)
        sid = request.path_params.get("sid")
        if sid and not is_session_id(sid) and not path.startswith("/api/hosts/"):
            return error_response("bad session id" if request.method == "POST" else "not found",
                                  400 if request.method == "POST" else 404)
        return None

    async def _body(self, request):
        if request.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return error_response("send application/json", 415)
        length = int(request.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return error_response("request body too large")
        raw = await request.body() if length else b"{}"
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            return error_response("body must be a JSON object")
        # Let FastAPI bind the declared Pydantic model even for a zero-byte POST.
        # These are Starlette's private request caches. uv.lock pins their shape;
        # upgrading Starlette/FastAPI is a contract change requiring HTTP parity.
        request._body = raw or b"{}"
        request._json = body
        return None
