"""Find Claude Code conversations on disk and summarize each one for the picker.

Transcripts can be many megabytes, so a file is only re-read when its size or
mtime changes, and each line is checked with a byte search before it is
parsed as JSON.
"""

from dataclasses import dataclass
from pathlib import Path
import json
import threading
import time

from .paths import PROJECTS_DIR, HOME
from .session_id import is_session_id

_TITLE_LEN = 120

# Claude Code writes compact JSON, so these byte markers find the records we
# need without parsing every line.
_CWD = b'"cwd":"'
_USER = b'"type":"user"'
_CUSTOM_TITLE = b'"type":"custom-title"'
_AI_TITLE = b'"type":"ai-title"'
_SUMMARY = b'"type":"summary"'
BRIDGE_RECORD = b'"type":"bridge-session"'
# Each record names the front end that wrote it: "cli" for a terminal, and
# "sdk-…" for headless runs (`claude -p`, the Agent SDK).
_ENTRYPOINT = b'"entrypoint":"'
_HEADLESS_PREFIX = b"sdk-"
# A Remote Control bridge id "cse_X" is the claude.ai session "session_X".
_BRIDGE_PREFIX = "cse_"
REMOTE_URL_BASE = "https://claude.ai/code/session_"


@dataclass(frozen=True)
class Transcript:
    session_id: str
    path: Path
    cwd: str
    title: str
    first_prompt: str
    mtime: float
    size: int
    # claude.ai link from the last Remote Control connection, if there was one.
    remote_url: str = ""
    # Every record came from a headless run: nobody ever typed in it.
    headless: bool = False

    @property
    def project(self) -> str:
        return display_path(self.cwd)


def display_path(path: str) -> str:
    """The path with the home folder shortened to ~."""
    home = str(HOME)
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def summarize(path: Path, *, metadata_only: bool = False) -> Transcript | None:
    """Read one transcript. Returns None for files with no conversation in them."""
    stat = path.stat()
    cwd = first_prompt = custom_title = ai_title = summary = bridge_id = ""
    headless_seen = interactive = False
    with open(path, "rb") as f:
        for line in f:
            if not interactive:
                entrypoint = _entrypoint(line)
                if entrypoint is not None:
                    headless_seen |= entrypoint.startswith(_HEADLESS_PREFIX)
                    interactive = not entrypoint.startswith(_HEADLESS_PREFIX)
            if not cwd and _CWD in line:
                cwd = str(_loads(line).get("cwd") or "")
            if _CUSTOM_TITLE in line:
                custom_title = str(_loads(line).get("customTitle") or custom_title)
            elif _AI_TITLE in line:
                ai_title = str(_loads(line).get("aiTitle") or ai_title)
            elif not metadata_only and _SUMMARY in line:
                summary = str(_loads(line).get("summary") or summary)
            elif BRIDGE_RECORD in line:
                bridge_id = str(_loads(line).get("bridgeSessionId") or bridge_id)
            elif not first_prompt and _USER in line:
                first_prompt = prompt_text(_loads(line))
    if metadata_only:
        # Prompts never leave a metadata-only read, but a transcript with none
        # (slash commands only, or opened and quit) is skipped as it is in full mode.
        title = custom_title or ai_title or ("(untitled)" if first_prompt else "")
        first_prompt = summary = ""
    else:
        title = custom_title or ai_title or summary or first_prompt
    if not title:
        return None
    return Transcript(
        session_id=path.stem,
        path=path,
        cwd=cwd,
        title=one_line(title),
        first_prompt=one_line(first_prompt),
        mtime=stat.st_mtime,
        size=stat.st_size,
        remote_url=_remote_url(bridge_id),
        headless=headless_seen and not interactive,
    )


def _entrypoint(line: bytes) -> bytes | None:
    """The record's entrypoint value, found by byte search, or None."""
    start = line.find(_ENTRYPOINT)
    if start < 0:
        return None
    start += len(_ENTRYPOINT)
    end = line.find(b'"', start)
    return line[start:end] if end > start else None


def custom_title_record(session_id: str, title: str) -> bytes:
    """The line Claude Code appends when a session is renamed."""
    record = {"type": "custom-title", "customTitle": title, "sessionId": session_id}
    return (json.dumps(record, separators=(",", ":")) + "\n").encode()


def _remote_url(bridge_id: str) -> str:
    if not bridge_id.startswith(_BRIDGE_PREFIX):
        return ""
    return REMOTE_URL_BASE + bridge_id[len(_BRIDGE_PREFIX):]


def _loads(line: bytes) -> dict:
    try:
        value = json.loads(line)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def prompt_text(record: dict) -> str:
    """The text the user typed, or "" for tool results, meta records and slash-command echoes."""
    if record.get("isMeta"):
        return ""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        content = " ".join(texts)
    if not isinstance(content, str):
        return ""
    text = content.strip()
    # Wrapped records such as <command-name>…</command-name> are not prompts.
    return "" if text.startswith("<") else text


def reply_text(record: dict) -> str:
    """The text an assistant record shows, without thinking or tool calls."""
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()


def age_label(mtime: float | None) -> str:
    """How long ago, in the largest whole unit: 3d, 5h, 12m or now."""
    if not mtime:
        return "-"
    secs = time.time() - mtime
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return "now"


def one_line(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _TITLE_LEN else text[: _TITLE_LEN - 1] + "…"


class TranscriptIndex:
    """All transcripts under the projects folder, cached by size and mtime."""

    def __init__(self, projects_dir: Path = PROJECTS_DIR, *, metadata_only: bool = False):
        self.projects_dir = Path(projects_dir)
        self.metadata_only = metadata_only
        self._cache: dict[Path, tuple[tuple[int, int], Transcript | None]] = {}
        self._lock = threading.Lock()

    def all(self) -> list[Transcript]:
        with self._lock:
            seen = set()
            found = []
            for path in self.projects_dir.glob("*/*.jsonl"):
                if not is_session_id(path.stem):
                    continue
                seen.add(path)
                transcript = self._summary(path)
                if transcript:
                    found.append(transcript)
            for stale in set(self._cache) - seen:
                del self._cache[stale]
        # A session resumed from another folder can leave a copy in each; the
        # most recently written one is the live conversation.
        newest: dict[str, Transcript] = {}
        for t in found:
            if t.session_id not in newest or t.mtime > newest[t.session_id].mtime:
                newest[t.session_id] = t
        return sorted(newest.values(), key=lambda t: t.mtime, reverse=True)

    def get(self, session_id: str) -> Transcript | None:
        return next((t for t in self.all() if t.session_id == session_id), None)

    def resolve(self, prefix: str) -> list[Transcript]:
        """Sessions whose id starts with prefix (for the CLI's short ids)."""
        return [t for t in self.all() if t.session_id.startswith(prefix)]

    def _summary(self, path: Path) -> Transcript | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        try:
            transcript = summarize(path, metadata_only=self.metadata_only)
        except OSError:
            transcript = None
        self._cache[path] = (key, transcript)
        return transcript
