"""HTTP/CLI-facing transcript search handlers, separate from web routing."""

from http import HTTPStatus
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from . import settings
from .launcher import resume_for_tool
from .search_index import SearchTimeout, TranscriptSearch
from .search_query import QueryError, SearchQuery, parse_query
from .search_records import SearchResult
from .session_id import is_session_id

_SEARCH_DETAIL = re.compile(r"^/api/search/(claude|codex)/([0-9a-f-]+)$")


class SearchHandlers:
    def __init__(self, index_file: Path, settings_provider=settings.load):
        self.index = TranscriptSearch(index_file)
        self.settings_provider = settings_provider

    def status(self) -> dict:
        return {"index": self.index.status()}

    def results(self, raw_query: str, session_rows=(), limit: int = 50) -> dict:
        query = parse_query(raw_query)
        try:
            local = self.index.search(query, limit=limit)
        except SearchTimeout as exc:
            raise QueryError(str(exc)) from exc
        current = self.settings_provider()
        for row in local:
            self._resume_command(row, current)
        remote = remote_title_matches(query, session_rows)
        results = sorted([*local, *remote], key=lambda row: row.get("matched_at") or row.get("mtime") or 0,
                         reverse=True)[:limit]
        return {"query": raw_query, "results": results, "index": self.index.status()}

    def detail(self, tool: str, session_id: str, raw_query: str) -> dict | None:
        query = parse_query(raw_query)
        try:
            detail = self.index.detail(query, tool, session_id)
        except SearchTimeout as exc:
            raise QueryError(str(exc)) from exc
        if detail:
            self._resume_command(detail["result"], self.settings_provider())
        return {"query": raw_query, "detail": detail, "index": self.index.status()} if detail else None

    @staticmethod
    def _resume_command(row: dict, current: settings.Settings) -> None:
        # Index metadata outlives install settings; resolve commands at response time.
        row["resume_cmd"] = resume_for_tool(row["tool"], row["id"], row["cwd"], current)


def route_search_get(raw_path: str, handlers: SearchHandlers, session_rows) -> tuple[dict, HTTPStatus] | None:
    """Handle the search GET routes; return None for the rest of web.py."""
    parsed = urlsplit(raw_path)
    if parsed.path == "/api/search/status":
        return handlers.status(), HTTPStatus.OK
    query = parse_qs(parsed.query).get("q", [""])[0]
    try:
        if parsed.path == "/api/search":
            return handlers.results(query, session_rows()), HTTPStatus.OK
        match = _SEARCH_DETAIL.match(parsed.path)
        if match and is_session_id(match.group(2)):
            result = handlers.detail(*match.groups(), query)
            return ((result, HTTPStatus.OK) if result else
                    ({"error": "no matching session"}, HTTPStatus.NOT_FOUND))
    except QueryError as exc:
        return {"error": str(exc)}, HTTPStatus.BAD_REQUEST
    return None


def remote_title_matches(query: SearchQuery, rows, now: float | None = None) -> list[SearchResult]:
    current = time.time() if now is None else now
    found: list[SearchResult] = []
    for row in rows:
        if not query.matches_remote(row, current):
            continue
        found.append({
            "tool": str(row.get("tool") or "claude"),
            "id": str(row.get("id") or ""),
            "short_id": str(row.get("short_id") or ""),
            "title": str(row.get("title") or "(untitled)"),
            "cwd": str(row.get("cwd") or ""),
            "project": str(row.get("project") or row.get("cwd") or ""),
            "mtime": row.get("mtime"),
            "count": 0,
            "matched_at": row.get("mtime"),
            "excerpt": "Title/folder match only; this machine's transcript is not on the Pi.",
            "resume_cmd": str(row.get("resume_cmd") or ""),
            "host": str(row.get("host") or ""),
            "title_only": True,
        })
    return found
