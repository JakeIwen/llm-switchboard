"""Shared record shapes and vocabulary for transcript full-text search."""

from dataclasses import dataclass
from enum import Enum
from typing import TypedDict


class SearchTool(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


class SearchRole(str, Enum):
    ME = "me"
    REPLY = "reply"
    TOOL = "tool"
    OTHER = "other"


class IndexState(str, Enum):
    MISSING = "missing"
    BUILDING = "building"
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class ExtractedText:
    role: SearchRole
    text: str


@dataclass(frozen=True)
class LineExtraction:
    chunks: tuple[ExtractedText, ...] = ()
    session_id: str = ""
    cwd: str = ""
    title: str = ""
    title_rank: int = 0
    timestamp: float | None = None


class SearchStatus(TypedDict):
    state: str
    sessions: int
    files: int
    last_updated: float | None
    started_at: float | None
    error: str


class SearchResult(TypedDict):
    tool: str
    id: str
    short_id: str
    title: str
    cwd: str
    project: str
    mtime: float | None
    count: int
    matched_at: float | None
    excerpt: str
    resume_cmd: str
    host: str
    title_only: bool


class ContextLine(TypedDict):
    role: str
    text: str
    at: float | None
    match: bool


class SearchContext(TypedDict):
    matched_at: float | None
    lines: list[ContextLine]


class SearchDetail(TypedDict):
    result: SearchResult
    contexts: list[SearchContext]
