"""Codex sessions ("threads") as the picker lists them, read from the Codex daemon.

The listing uses the daemon's state database only (useStateDbOnly), which
answers in milliseconds where a full scan of the rollout files takes
seconds. It's cached briefly, since the page polls every few seconds, and the
last good listing is kept while the daemon is unreachable (it restarts itself
on Codex updates).
"""

from dataclasses import dataclass
import os
import threading
import time

from .codex_client import CodexClient, CodexError, CodexUnavailable
from .conversation import clip_message
from .transcripts import display_path, one_line

_TTL_S = 3.0
_PAGE = 100


class CodexState:
    """Codex's thread status types (thread.status.type)."""
    NOT_LOADED = "notLoaded"
    IDLE = "idle"
    ACTIVE = "active"  # working on a turn
    SYSTEM_ERROR = "systemError"


@dataclass(frozen=True)
class CodexThread:
    thread_id: str
    cwd: str
    title: str
    first_prompt: str
    mtime: float | None
    size: int | None
    state: str

    @property
    def project(self) -> str:
        return display_path(self.cwd)


def codex_short_id(thread_id: str) -> str:
    """Codex ids are time-ordered (UUIDv7), so threads started within a minute
    or so share their first 8 characters; the last 8 are random."""
    return thread_id[-8:]


def thread_from_api(t: dict) -> CodexThread | None:
    """One thread from thread/list, or None for ones the picker doesn't show
    (subagent threads and ephemeral ones)."""
    if t.get("parentThreadId") or t.get("ephemeral") or not t.get("id"):
        return None
    mtime = size = None
    path = t.get("path")
    if path:
        try:
            st = os.stat(path)
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            pass
    preview = one_line(str(t.get("preview") or ""))
    return CodexThread(
        thread_id=t["id"],
        cwd=str(t.get("cwd") or ""),
        title=one_line(str(t.get("name") or "")) or preview or "(untitled)",
        first_prompt=preview,
        mtime=mtime or t.get("recencyAt") or t.get("updatedAt"),
        size=size,
        state=str((t.get("status") or {}).get("type") or CodexState.NOT_LOADED),
    )


class CodexCatalog:
    def __init__(self, client: CodexClient, ttl_s: float = _TTL_S, clock=time.monotonic):
        self.client = client
        self.ttl_s = ttl_s
        self.clock = clock
        self.error = ""
        self._threads: dict[str, CodexThread] = {}
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def threads(self) -> list[CodexThread]:
        with self._lock:
            if self._fetched_at is None or self.clock() - self._fetched_at >= self.ttl_s:
                self._refresh()
            return list(self._threads.values())

    def get(self, thread_id: str) -> CodexThread | None:
        return next((t for t in self.threads() if t.thread_id == thread_id), None)

    def invalidate(self) -> None:
        with self._lock:
            self._fetched_at = None

    def messages(self, thread_id: str, limit: int = 8) -> list[dict]:
        """[{role, text, at}] for the last prompts and replies, oldest first."""
        result = self.client.call("thread/turns/list", {"threadId": thread_id, "limit": limit, "sortDirection": "desc"})
        messages = []
        for turn in reversed((result or {}).get("data") or []):
            for item in turn.get("items") or []:
                if item.get("type") == "userMessage":
                    role = "user"
                    text = "\n".join(c.get("text", "") for c in item.get("content") or [] if c.get("type") == "text")
                elif item.get("type") == "agentMessage":
                    role, text = "assistant", str(item.get("text") or "")
                else:
                    continue
                if text.strip():
                    messages.append({"role": role, "text": clip_message(text.strip()), "at": turn.get("startedAt") or ""})
        return messages[-limit:]

    def _refresh(self) -> None:
        try:
            found, cursor = {}, None
            with self.client.connect() as conn:
                while True:
                    page = conn.call("thread/list", {"limit": _PAGE, "cursor": cursor, "useStateDbOnly": True}) or {}
                    for raw in page.get("data") or []:
                        thread = thread_from_api(raw)
                        if thread:
                            found[thread.thread_id] = thread
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
        except (CodexUnavailable, CodexError) as e:
            self.error = str(e)
            self._fetched_at = self.clock()
            return
        self._threads, self.error, self._fetched_at = found, "", self.clock()


def codex_names(catalog: CodexCatalog):
    """Codex sessions for the per-folder unique-name check (names.Names)."""
    def source():
        return ((t.thread_id, t.cwd, t.title) for t in catalog.threads())
    return source
