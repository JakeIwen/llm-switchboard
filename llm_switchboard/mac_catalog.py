"""Mac metadata sources: titles (an unnamed session's first prompt, one line at
most) but no prompt, message or screen fields."""

import json
from pathlib import Path

from .codex_client import CodexClient
from .codex_threads import CodexCatalog, CodexState, is_headless_thread, is_subagent_meta
from .host_protocol import MAX_ROWS, SessionState, row, session_id
from .mac_registry import mac_live_processes
from .paths import CODEX_DIR, CODEX_SOCKET
from .search_extract import codex_first_prompt
from .transcripts import TranscriptIndex, one_line


class MacCatalog:
    def __init__(self, index=None, codex=None, codex_dir=CODEX_DIR, registry=mac_live_processes):
        self.index = index or TranscriptIndex(metadata_only=True)
        self.codex = codex or CodexCatalog(CodexClient(CODEX_SOCKET), metadata_only=True)
        self.codex_dir, self.registry = Path(codex_dir), registry
        self._codex_prompts: dict[Path, tuple[str, int]] = {}

    def rows(self) -> list[dict]:
        live = {p.session_id: p.status for p in self.registry()}
        rows = [dict(id=t.session_id, tool="claude", title=t.title, cwd=t.cwd, mtime=t.mtime,
                     size=t.size, state=live.get(t.session_id, SessionState.CLOSED), mutable=True,
                     headless=t.headless)
                for t in self.index.all()]
        threads = self.codex.threads()
        if not self.codex.error and self.codex.client.enabled:
            rows += [dict(id=t.thread_id, tool="codex", title=t.title, cwd=t.cwd, mtime=t.mtime,
                          size=t.size, state=SessionState.BUSY if t.state == CodexState.ACTIVE else SessionState.CLOSED,
                          mutable=True, headless=t.headless) for t in threads]
        else:
            rows += codex_disk_rows(self.codex_dir, self._codex_prompts)
        valid = []
        for candidate in rows[:MAX_ROWS]:
            try:
                valid.append(row(candidate))
            except ValueError:
                continue  # A stale/malformed local record must not block other sessions.
        return valid


def codex_disk_rows(root: Path, prompts: dict | None = None) -> list[dict]:
    """Read session names, rollout headers and, for an unnamed session, its first
    prompt as the title; never modify Codex's files."""
    titles, found = {}, {}
    prompts = {} if prompts is None else prompts
    try:
        with open(root / "session_index.jsonl") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("thread_name"):
                        titles[session_id(entry.get("id"))] = one_line(str(entry["thread_name"]))
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    for path in (root / "sessions").glob("**/*.jsonl"):
        try:
            with open(path, "rb") as f:
                header = json.loads(f.readline(65537))
            meta = header.get("payload", {})
            if header.get("type") != "session_meta" or not isinstance(meta, dict):
                continue
            if is_subagent_meta(meta):
                continue
            sid = session_id(meta.get("id"))
            stat = path.stat()
            title = titles.get(sid) or _first_prompt(path, stat.st_size, prompts) or "(untitled)"
            candidate = dict(id=sid, tool="codex", title=title, cwd=meta.get("cwd"),
                             mtime=stat.st_mtime, size=stat.st_size, state=SessionState.CLOSED, mutable=False,
                             headless=is_headless_thread(meta.get("source"), meta.get("originator")))
            found[sid] = row(candidate)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if len(found) >= MAX_ROWS:
            break
    return list(found.values())


def _first_prompt(path: Path, size: int, cache: dict) -> str:
    """The agent pushes every 10 s: a found prompt never changes, and a rollout
    with none yet is read again only once it has grown."""
    known = cache.get(path)
    if known is not None and (known[0] or known[1] == size):
        return known[0]
    prompt = codex_first_prompt(path)
    cache[path] = (prompt, size)
    return prompt
