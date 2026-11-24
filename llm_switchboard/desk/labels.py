"""A desk target's label and folder: its transcript's title, else the
folder's name, else the session id's first 8 characters.

Transcripts grow to many megabytes, so a label is worked out from a bounded
read (the first `LABEL_HEAD_BYTES` and the last `LABEL_TAIL_BYTES`) and only
again when the file's identity (path, inode, size, mtime) changed. A custom
title set in the middle of a huge transcript and never repeated can be
missed; the label then falls back to the AI title, summary, first prompt or
folder. The hub calls this from its recheck loop and on registration, never
from the state long-poll.
"""

from collections import OrderedDict
import contextlib
from dataclasses import dataclass
from pathlib import Path

from ..transcripts import one_line, prompt_text
from .config import DeskConfig
from .model import LABEL_HEAD_BYTES, LABEL_TAIL_BYTES, MAX_LABEL_CACHE
from .peer import MAX_SESSION_FILE_DEPTH
from .shallow_json import loads_shallow

# The same compact-JSON byte markers transcripts.summarize uses, so a line is
# parsed only when it can matter.
_CWD = b'"cwd":"'
_USER = b'"type":"user"'
_CUSTOM_TITLE = b'"type":"custom-title"'
_AI_TITLE = b'"type":"ai-title"'
_SUMMARY = b'"type":"summary"'
# Claude Code's session files are a few hundred bytes.
_MAX_SESSION_FILE_BYTES = 64 * 1024
# A transcript record nests a few levels (message, content, a tool's input);
# a deeper line is skipped like any line this reader can't use.
MAX_TRANSCRIPT_RECORD_DEPTH = 64


def encodable(value: str) -> str:
    """`value` with anything strict UTF-8 can't encode (a lone surrogate from
    a JSON escape) replaced, so one odd folder name can't break the state
    snapshot every tablet reads."""
    return value.encode("utf-8", "replace").decode("utf-8")


def _record(line: bytes) -> dict:
    with contextlib.suppress(ValueError):
        value = loads_shallow(line, MAX_TRANSCRIPT_RECORD_DEPTH)
        if isinstance(value, dict):
            return value
    return {}


def _bounded_lines(path: Path, size: int) -> list[bytes]:
    """Every line of a small file; else the whole lines in its head and tail."""
    with open(path, "rb") as f:
        if size <= LABEL_HEAD_BYTES + LABEL_TAIL_BYTES:
            return f.read(LABEL_HEAD_BYTES + LABEL_TAIL_BYTES).split(b"\n")
        head = f.read(LABEL_HEAD_BYTES)
        f.seek(size - LABEL_TAIL_BYTES)
        tail = f.read(LABEL_TAIL_BYTES)
    # The head ends mid-line and the tail starts mid-line.
    return head.split(b"\n")[:-1] + tail.split(b"\n")[1:]


def read_title_and_cwd(path: Path, size: int) -> tuple[str, str]:
    found = {"cwd": "", "first_prompt": "", "custom": "", "ai": "", "summary": ""}
    for line in _bounded_lines(path, size):
        try:
            _read_line_into(line, found)
        except Exception:
            # Valid JSON in a shape this reader doesn't expect: skip the line.
            continue
    title = found["custom"] or found["ai"] or found["summary"] or found["first_prompt"]
    return one_line(title), found["cwd"]


def _read_line_into(line: bytes, found: dict) -> None:
    if not found["cwd"] and _CWD in line:
        found["cwd"] = str(_record(line).get("cwd") or "")
    if _CUSTOM_TITLE in line:
        found["custom"] = str(_record(line).get("customTitle") or found["custom"])
    elif _AI_TITLE in line:
        found["ai"] = str(_record(line).get("aiTitle") or found["ai"])
    elif _SUMMARY in line:
        found["summary"] = str(_record(line).get("summary") or found["summary"])
    elif not found["first_prompt"] and _USER in line:
        found["first_prompt"] = prompt_text(_record(line))


@dataclass(frozen=True)
class _Cached:
    path: Path | None
    # (path, inode, size, mtime_ns) when the label was read; None: no transcript.
    key: tuple | None
    label: str
    cwd: str


class SessionLabels:
    def __init__(self, config: DeskConfig):
        self.projects_dir = config.home / ".claude" / "projects"
        self.sessions_dir = config.claude_sessions_dir
        self._cache: OrderedDict[str, _Cached] = OrderedDict()

    def describe(self, session_id: str, claude_pid: int | None = None) -> tuple[str, str]:
        cached = self._cache.get(session_id)
        path = cached.path if cached else None
        key = self._key(path)
        if key is None:
            # Not found yet, or moved: one glob of the project folders.
            path = self._find(session_id)
            key = self._key(path)
        if cached and key is not None and cached.key == key:
            return cached.label, cached.cwd
        title = cwd = ""
        if key is not None:
            with contextlib.suppress(OSError):
                title, cwd = read_title_and_cwd(path, key[2])
        if not cwd and claude_pid:
            cwd = self._session_cwd(claude_pid)
        title, cwd = encodable(title), encodable(cwd)
        label = title or (Path(cwd).name if cwd else "") or session_id[:8]
        self._cache[session_id] = _Cached(path if key else None, key, label, cwd)
        self._cache.move_to_end(session_id)
        while len(self._cache) > MAX_LABEL_CACHE:
            self._cache.popitem(last=False)
        return label, cwd

    @staticmethod
    def _key(path: Path | None):
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _find(self, session_id: str) -> Path | None:
        candidates = []
        for path in self.projects_dir.glob(f"*/{session_id}.jsonl"):
            with contextlib.suppress(OSError):
                candidates.append((path.stat().st_mtime, path))
        return max(candidates)[1] if candidates else None

    def _session_cwd(self, claude_pid: int) -> str:
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            with open(self.sessions_dir / f"{int(claude_pid)}.json", "rb") as f:
                return str(loads_shallow(f.read(_MAX_SESSION_FILE_BYTES), MAX_SESSION_FILE_DEPTH).get("cwd") or "")
        return ""
