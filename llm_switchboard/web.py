"""FastAPI transport for the picker, preserving the existing HTTP contract.

JSON-only POSTs deliberately have no CORS support: a foreign web page cannot
submit an action without a preflight, which this application never approves.
"""

import json
from pathlib import Path
import re

from fastapi import Body, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from . import next_page
from .host_actions_protocol import ActionKind
from .host_web import action_route
from .http.lifespan import service_lifespan
from .http import requests as bodies
from .http import responses as schemas
from .http.transport import CompatibilityRoute, RawPathMiddleware, error_response, json_response, validation_error
from .search_api import route_search_get
# Permanent public imports for CLI and existing callers.
from .web_app import App, make_app

STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_RE = re.compile(r"^/static/([a-z][a-z0-9-]*\.(js|css))$")
_STATIC_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}
LOCAL_ACTIONS = {
    "enable": (bodies.ActionBody, schemas.ToggleResponse),
    "disable": (bodies.ActionBody, schemas.ToggleResponse),
    "restart": (bodies.ActionBody, schemas.OkResponse),
    "name": (bodies.NameBody, schemas.ToggleResponse),
    "rename": (bodies.RenameBody, schemas.RenameResponse),
    "delete": (bodies.ActionBody, schemas.DeleteResponse),
    "restore": (bodies.ActionBody, schemas.RestoreResponse),
    "move": (bodies.MoveBody, schemas.MoveResponse),
    "fork": (bodies.ForkBody, schemas.ForkResponse),
}
REMOTE_BODIES = {
    ActionKind.RENAME: bodies.RenameBody, ActionKind.DELETE: bodies.ActionBody,
    ActionKind.RESTORE: bodies.ActionBody, ActionKind.MOVE: bodies.MoveBody,
    ActionKind.FORK: bodies.RemoteForkBody, ActionKind.FORK_PI: bodies.RenameBody,
}
ERRORS = {code: {"model": schemas.ErrorResponse} for code in (400, 404, 409, 415, 503)}


def create_http_app(domain: App | None = None, *, manage_lifecycle=False) -> FastAPI:
    app = FastAPI(title="llm-switchboard", version="1", redirect_slashes=False,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=service_lifespan if manage_lifecycle else None)
    app.state.domain = domain
    app.add_middleware(RawPathMiddleware)
    app.router.route_class = CompatibilityRoute
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, lambda request, exc: error_response("not found", 404))
    _read_routes(app)
    _write_routes(app)
    _static_routes(app)
    return app


def _add(app, path, endpoint, model, method="GET", **options):
    app.add_api_route(path, endpoint, methods=[method], response_model=model,
                      responses=ERRORS, **options)


def _read_routes(app):
    for path, method, model in [
        ("settings", "settings_payload", schemas.SettingsResponse),
        ("sessions", "list_sessions", schemas.SessionsResponse),
        ("folders", "list_folders", schemas.FoldersResponse),
        ("trash", "list_trash", schemas.TrashResponse),
    ]:
        _add(app, f"/api/{path}", _read_endpoint(method, model), model)
    for action, model in [("screen", schemas.ScreenResponse), ("messages", schemas.MessagesResponse)]:
        _add(app, f"/api/sessions/{{sid}}/{action}", _session_read(action, model), model)
    for path, model in [("/api/search", schemas.SearchResponse),
                        ("/api/search/status", schemas.SearchStatusResponse),
                        ("/api/search/claude/{sid}", schemas.SearchDetailResponse),
                        ("/api/search/codex/{sid}", schemas.SearchDetailResponse)]:
        _add(app, path, _search_endpoint(model), model)


def _read_endpoint(method, model):
    def read(request: Request):
        return json_response(getattr(request.app.state.domain, method)(), model)
    read.__name__ = method
    return read


def _session_read(action, model):
    def read(request: Request, sid: str):
        return json_response(getattr(request.app.state.domain, action)(sid), model)
    read.__name__ = action
    return read


def _search_endpoint(model):
    def respond(request):
        domain = request.app.state.domain
        result = route_search_get(request.url.path + "?" + request.url.query, domain.search,
                                  lambda: domain.list_sessions()["sessions"])
        if result is None:
            return error_response("not found", 404)
        payload, status = result
        return json_response(payload, model if status == 200 else schemas.ErrorResponse, status)

    def search(request: Request, q: str = ""):
        return respond(request)

    def detail(request: Request, sid: str, q: str = ""):
        return respond(request)

    def status(request: Request):
        return respond(request)

    endpoint = {schemas.SearchResponse: search, schemas.SearchDetailResponse: detail,
                schemas.SearchStatusResponse: status}[model]
    endpoint.__name__ = model.__name__
    return endpoint


def _write_routes(app):
    for path, method, body, model in [
        ("/api/settings", "settings_payload", bodies.SettingsBody, schemas.SettingsResponse),
        ("/api/sessions/new", "create", bodies.CreateBody, schemas.CreateResponse),
        ("/api/trash/empty", "empty_trash", bodies.ActionBody, schemas.EmptyTrashResponse),
    ]:
        _add(app, path, _write_endpoint(method, body, model), model, "POST")
    for action, (body, model) in LOCAL_ACTIONS.items():
        _add(app, f"/api/sessions/{{sid}}/{action}", _action_endpoint(action, body, model), model, "POST")
    for action, body in REMOTE_BODIES.items():
        _add(app, f"/api/hosts/{{host}}/sessions/{{sid}}/{action}",
             _remote_endpoint(action, body), schemas.RemoteActionResponse, "POST")
    _add(app, "/api/hosts/{host}/sessions/{sid}/{action}", _remote_endpoint(None, bodies.ActionBody),
         schemas.RemoteActionResponse, "POST", include_in_schema=False)


def _write_endpoint(method, body_model, response_model):
    def write(request: Request, body=Body()):
        domain = request.app.state.domain
        result = domain.empty_trash() if method == "empty_trash" else getattr(domain, method)(body.payload())
        return json_response(result, response_model)
    write.__annotations__["body"] = body_model
    write.__name__ = method
    return write


def _action_endpoint(action, body_model, response_model):
    def write(request: Request, sid: str, body=Body()):
        return json_response(request.app.state.domain.act(sid, action, body.payload()), response_model)
    write.__annotations__["body"] = body_model
    write.__name__ = action
    return write


def _remote_endpoint(kind, body_model):
    def write(request: Request, host: str, sid: str, body=Body()):
        route = action_route(request.url.path)
        if route is None:
            return error_response("not found", 404)
        return json_response(request.app.state.domain.queue_remote(*route, body.payload()), schemas.RemoteActionResponse)
    write.__annotations__["body"] = body_model
    write.__name__ = f"host_{kind or 'action'}"
    return write


def _static_routes(app):
    def page(request: Request):
        path = request.url.path
        if path in ("/", "/index.html"):
            return _file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        match = _STATIC_RE.fullmatch(path)
        if match and (STATIC_DIR / match[1]).is_file():
            return _file(STATIC_DIR / match[1], _STATIC_TYPES[match[2]])
        return error_response("not found", 404)
    for path in ("/", "/index.html", "/static/{filename:path}"):
        app.add_api_route(path, page, methods=["GET"], response_class=Response, include_in_schema=False)
    for path in (next_page.PREFIX.rstrip("/"), next_page.PREFIX + "{filename:path}"):
        app.add_api_route(path, _preview_page, methods=["GET"], response_class=Response, include_in_schema=False)


def _preview_page(request: Request):
    raw_path = request.scope["raw_path"].decode("ascii")
    page = next_page.resolve(raw_path)
    if page is None:
        return error_response("not found", 404)
    return Response(page.body, status_code=page.status,
                    headers={"Content-Type": page.content_type, "Cache-Control": page.cache_control, **page.headers})


def _file(path, content_type):
    return Response(path.read_bytes(), headers={"Content-Type": content_type, "Cache-Control": "no-store"})


def make_server(app: App, host: str, port: int):
    """Keep the one-address integration interface, now backed by Uvicorn."""
    from .http.server import BoundServer
    return BoundServer(create_http_app(app), host, port)


def openapi_schema() -> dict:
    schema = create_http_app().openapi()
    for item in schema["paths"].values():
        for operation in item.values():
            operation.get("responses", {}).pop("422", None)
    return schema


def openapi_main() -> int:
    print(json.dumps(openapi_schema(), sort_keys=True, indent=2))
    return 0
