"""The tablet page's files as this hub serves them, and their build id
(docs/voice-desk.md, "Conversation mode": one current page).

The hub reads every page file once, when it is made, and serves those bytes
for its whole run, so the build it reports is exactly what it serves. The
build is the first 16 hex digits of a SHA-256 over every route and its file.
`index.html` carries it in `<meta name="desk-build">` (the placeholder below
is replaced as it is served) and the page sends it with its pairing claim;
a claim from any other build is refused (`PAGE_OUT_OF_DATE`), so a tab
loaded before an install can't pair with the new hub.

Bound: the files are the fixed `STATIC_ROUTES` list, a few hundred KiB at
most, held in memory only.
"""

import hashlib
from pathlib import Path

from .refusals import Refusal

BUILD_PLACEHOLDER = b"@DESK_BUILD@"
BUILD_HEX_DIGITS = 16
PAGE_OUT_OF_DATE = Refusal.PAGE_OUT_OF_DATE  # its former name, kept for callers
_HTML_ROUTE = "/desk"


class PageFiles:
    def __init__(self, routes: dict[str, tuple[Path, str]]):
        digest = hashlib.sha256()
        raw: dict[str, tuple[bytes | None, str]] = {}
        for route in sorted(routes):
            file, content_type = routes[route]
            try:
                data = file.read_bytes()
            except OSError:
                data = None
            raw[route] = (data, content_type)
            digest.update(route.encode() + b"\0" + (b"-" if data is None else hashlib.sha256(data).digest()))
        self.build = digest.hexdigest()[:BUILD_HEX_DIGITS]
        index, content_type = raw.get(_HTML_ROUTE, (None, ""))
        if index is not None:
            raw[_HTML_ROUTE] = (index.replace(BUILD_PLACEHOLDER, self.build.encode()), content_type)
        self._files = raw

    def get(self, route: str) -> tuple[bytes, str] | None:
        """The bytes and content type served at `route`; None when the file
        was missing at start (a 404)."""
        data, content_type = self._files.get(route, (None, ""))
        return None if data is None else (data, content_type)
