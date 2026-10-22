"""Versioned metadata-only SSH exchange. Validators discard unknown row fields."""

from enum import StrEnum
import math
import re
from typing import TypedDict

from .session_id import is_session_id
from .session_names import MAX_TITLE_LEN

VERSION = 1
PUSH_INTERVAL = 10
OFFLINE_AFTER = 35
MAX_ROWS = 5000
MAX_METADATA_BYTES = 4 * 1024 * 1024
HOST_PATTERN = r"[a-z][a-z0-9-]{0,31}"
# A Mac home folder; /Users/Shared is nobody's.
MAC_HOME = re.compile(r"^/Users/(?!Shared(?:/|$))[^/]+(?=/|$)")


class SessionState(StrEnum):
    CLOSED = "closed"
    IDLE = "idle"
    BUSY = "busy"


class HostRow(TypedDict):
    id: str
    tool: str
    title: str
    cwd: str
    mtime: float | None
    size: int | None
    state: str
    mutable: bool


class Push(TypedDict):
    version: int
    rows: list[HostRow]


class Reply(TypedDict):
    version: int
    actions: list[dict]


def host_name(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(HOST_PATTERN, value):
        raise ValueError("host must be a lowercase name (letters, digits and hyphens; at most 32 characters)")
    return value


def session_id(value) -> str:
    if not isinstance(value, str) or not is_session_id(value):
        raise ValueError("bad session id")
    return value


def text(value, name: str, limit: int, *, empty=False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not value and not empty):
        raise ValueError(f"{name} must be text of at most {limit} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def folder(value) -> str:
    value = text(value, "folder", 4096)
    if not value.startswith("/") or ".." in value.split("/"):
        raise ValueError("folder must be an absolute path without '..'")
    return value


def number(value, name: str, *, integer=False):
    if value is None:
        return None
    if type(value) not in ((int,) if integer else (int, float)) or value < 0 or value > 2**53 - 1 or not math.isfinite(value):
        raise ValueError(f"invalid {name}")
    return value


def row(raw) -> HostRow:
    if not isinstance(raw, dict):
        raise ValueError("row must be an object")
    if raw.get("tool") not in ("claude", "codex"):
        raise ValueError("unknown session tool")
    if not isinstance(raw.get("state"), str) or raw["state"] not in set(SessionState):
        raise ValueError("unknown session state")
    if type(raw.get("mutable")) is not bool:
        raise ValueError("mutable must be boolean")
    return HostRow(id=session_id(raw.get("id")), tool=raw["tool"],
                   title=text(raw.get("title"), "title", MAX_TITLE_LEN), cwd=folder(raw.get("cwd")),
                   mtime=number(raw.get("mtime"), "mtime"), size=number(raw.get("size"), "size", integer=True),
                   state=raw["state"], mutable=raw["mutable"])


def push(raw) -> Push:
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != VERSION:
        raise ValueError("unsupported host exchange version")
    rows = raw.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError(f"rows must be a list of at most {MAX_ROWS} entries")
    clean = [row(r) for r in rows]
    if len({r["id"] for r in clean}) != len(clean):
        raise ValueError("duplicate session id")
    if raw.get("uploads"):
        raise ValueError("unsolicited transcript upload")
    return Push(version=VERSION, rows=clean)
