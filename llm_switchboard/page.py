"""The picker (web/, built into web/dist) served at the root.

Hashed build assets are cached for good; everything else is no-store, so a
phone never keeps a stale index after a deploy. Former preview URLs redirect
without involving the API router.
"""

from dataclasses import dataclass, field
from pathlib import Path
import json
import re

DIST_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"
PREVIEW_PREFIX = "/next/"

# One or two plain path segments (`theme.js`, `assets/index-Ab12.js`): no dot
# segments, no encoded slashes, nothing outside dist.
_FILE_RE = re.compile(r"^(?:assets/)?[A-Za-z0-9][A-Za-z0-9._-]*$")
_TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "json": "application/json",
    "svg": "image/svg+xml",
    "png": "image/png",
    "ico": "image/x-icon",
    "woff2": "font/woff2",
    "txt": "text/plain; charset=utf-8",
}
NO_STORE = "no-store"
IMMUTABLE = "public, max-age=31536000, immutable"
UNBUILT = "The page isn't built yet: run `npm ci && npm run build` in web/.\n"


@dataclass(frozen=True)
class PageResponse:
    status: int
    content_type: str
    body: bytes
    cache_control: str = NO_STORE
    headers: dict[str, str] = field(default_factory=dict)


def _not_found() -> PageResponse:
    return PageResponse(404, "application/json", json.dumps({"error": "not found"}).encode())


def resolve(raw_path: str, dist: Path | None = None) -> PageResponse | None:
    """What to send for `raw_path`, or None for API requests."""
    path, separator, query = raw_path.split("#", 1)[0].partition("?")
    if path == "/api" or path.startswith("/api/") or not path.startswith("/"):
        return None
    if path == PREVIEW_PREFIX.rstrip("/") or path.startswith(PREVIEW_PREFIX):
        rest = path[len(PREVIEW_PREFIX):]
        # A doubled separator must not turn Location into an external URL.
        if rest.startswith(("/", "\\")):
            return _not_found()
        location = "/" + rest + separator + query
        return PageResponse(308, "text/plain; charset=utf-8", b"", headers={"Location": location})
    dist = DIST_DIR if dist is None else dist
    index = dist / "index.html"
    if not index.is_file():
        return PageResponse(503, "text/plain; charset=utf-8", UNBUILT.encode())
    rest = path[1:]
    if rest in ("", "index.html"):
        return PageResponse(200, _TYPES["html"], index.read_bytes())
    if not _FILE_RE.match(rest):
        return _not_found()
    ext = rest.rsplit(".", 1)[-1] if "." in rest else ""
    target = dist / rest
    if not ext:
        # A client-side route: the page decides what to show.
        return PageResponse(200, _TYPES["html"], index.read_bytes())
    if ext not in _TYPES or not target.is_file():
        return _not_found()
    cache = IMMUTABLE if rest.startswith("assets/") else NO_STORE
    return PageResponse(200, _TYPES[ext], target.read_bytes(), cache)
