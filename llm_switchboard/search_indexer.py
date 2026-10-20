"""Incrementally build the shared transcript FTS index."""

from dataclasses import dataclass
from pathlib import Path
import fcntl
import json
import re
import sqlite3
import time

from .search_extract import (codex_line_may_matter, codex_names, extract_claude_record,
                             extract_codex_record)
from .search_index import drop_fts_triggers, open_writer, rebuild_fts, write_status
from .search_records import IndexState, SearchTool
from .session_id import is_session_id

_CODEX_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


@dataclass(frozen=True)
class IndexStats:
    files: int
    sessions: int
    changed: int
    removed: int
    bytes_read: int
    elapsed_s: float
    already_running: bool = False


def run_index(index_file: Path, claude_projects: Path, codex_dir: Path,
              pause_s: float = 0.01) -> IndexStats:
    """Index complete appended JSONL lines; one non-blocking process owns the pass."""
    started = time.time()
    lock_path = Path(index_file).with_suffix(Path(index_file).suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return IndexStats(0, 0, 0, 0, 0, time.time() - started, already_running=True)
        return _run_locked(Path(index_file), Path(claude_projects), Path(codex_dir), pause_s, started)


def _run_locked(index_file: Path, claude_projects: Path, codex_dir: Path,
                pause_s: float, started: float) -> IndexStats:
    conn = open_writer(index_file)
    bulk_build = _prepare_bulk_build(conn)
    write_status(conn, IndexState.BUILDING, started_at=started, error="")
    changed = removed = bytes_read = 0
    try:
        discovered = _discover(claude_projects, codex_dir)
        seen = {str(path) for _, path in discovered}
        known = {row["path"] for row in conn.execute("SELECT path FROM files")}
        for tool, path in discovered:
            did_change, read = _index_file(conn, tool, path)
            changed += int(did_change)
            bytes_read += read
            if pause_s:
                time.sleep(pause_s)
        for stale in known - seen:
            conn.execute("DELETE FROM files WHERE path=?", (stale,))
            removed += 1
        if bulk_build:
            _finish_bulk_build(conn)
        names_changed = _apply_codex_names(conn, codex_names(codex_dir / "session_index.jsonl"))
        current_sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        if changed or removed or names_changed or (not current_sessions and seen):
            sessions = _refresh_sessions(conn)
        else:
            sessions = current_sessions
        files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
        conn.commit()
        write_status(conn, IndexState.READY, sessions=sessions, files=files,
                     last_updated=time.time(), error="")
        return IndexStats(files, sessions, changed, removed, bytes_read, time.time() - started)
    except Exception as exc:
        conn.rollback()
        write_status(conn, IndexState.ERROR, error=str(exc))
        raise
    finally:
        conn.close()


def _prepare_bulk_build(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM index_meta WHERE key='bulk_build'").fetchone()
    bulk = bool(row and row[0] == "1")
    if not bulk:
        empty = conn.execute(
            "SELECT NOT EXISTS(SELECT 1 FROM entries) AND NOT EXISTS(SELECT 1 FROM files)"
        ).fetchone()[0]
        if empty:
            conn.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES ('bulk_build', '1')")
            conn.commit()
            bulk = True
    if bulk:
        # Loading external content first lets FTS5 tokenize it in one C-side pass.
        drop_fts_triggers(conn)
    return bulk


def _finish_bulk_build(conn: sqlite3.Connection) -> None:
    rebuild_fts(conn)
    conn.execute("DELETE FROM index_meta WHERE key='bulk_build'")
    conn.commit()


def _discover(claude_projects: Path, codex_dir: Path) -> list[tuple[SearchTool, Path]]:
    found: list[tuple[SearchTool, Path]] = []
    if claude_projects.is_dir():
        found.extend((SearchTool.CLAUDE, path) for path in claude_projects.glob("*/*.jsonl")
                     if path.is_file() and is_session_id(path.stem))
    sessions = codex_dir / "sessions"
    if sessions.is_dir():
        found.extend((SearchTool.CODEX, path) for path in sessions.rglob("*.jsonl") if path.is_file())
    return sorted(found, key=lambda item: str(item[1]))


def _index_file(conn: sqlite3.Connection, tool: SearchTool, path: Path) -> tuple[bool, int]:
    try:
        stat = path.stat()
    except OSError:
        return False, 0
    old = conn.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
    rebuild = old is None or old["inode"] != stat.st_ino or stat.st_size < old["size"]
    if old and not rebuild and old["mtime_ns"] == stat.st_mtime_ns and old["byte_offset"] == stat.st_size:
        return False, 0
    conn.execute("BEGIN")
    if rebuild:
        if old:
            conn.execute("DELETE FROM files WHERE path=?", (str(path),))
        session_id = path.stem if tool == SearchTool.CLAUDE else _codex_id_from_name(path)
        conn.execute(
            "INSERT INTO files(path, tool, session_id, inode, size, mtime_ns, modified, byte_offset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (str(path), tool.value, session_id, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_mtime),
        )
        offset, ordinal, cwd, title, title_rank = 0, 0, "", "", 0
    else:
        offset = old["byte_offset"]
        ordinal = conn.execute("SELECT COALESCE(max(ordinal), -1) + 1 FROM entries WHERE file_path=?",
                               (str(path),)).fetchone()[0]
        cwd, title, title_rank = old["cwd"], old["title"], old["title_rank"]
    session_id = (old["session_id"] if old and not rebuild else
                  (path.stem if tool == SearchTool.CLAUDE else _codex_id_from_name(path)))
    read_to, bytes_read, session_id, cwd, title, title_rank = _append_lines(
        conn, tool, path, offset, ordinal, session_id, cwd, title, title_rank,
    )
    conn.execute(
        "UPDATE files SET session_id=?, cwd=?, title=?, title_rank=?, inode=?, size=?, mtime_ns=?, "
        "modified=?, byte_offset=? WHERE path=?",
        (session_id, cwd, title, title_rank, stat.st_ino, stat.st_size, stat.st_mtime_ns,
         stat.st_mtime, read_to, str(path)),
    )
    conn.commit()
    return True, bytes_read


def _append_lines(conn: sqlite3.Connection, tool: SearchTool, path: Path, offset: int, ordinal: int,
                  session_id: str, cwd: str, title: str, title_rank: int) -> tuple[int, int, str, str, str, int]:
    extractor = extract_claude_record if tool == SearchTool.CLAUDE else extract_codex_record
    may_matter = codex_line_may_matter if tool == SearchTool.CODEX else None
    start_offset = offset
    # A rollout's first session_meta names it. Forks and subagents copy their
    # ancestors' meta lines after it, so later ones must not rename the file.
    named = offset > 0
    with path.open("rb") as source:
        source.seek(offset)
        while True:
            line_start = source.tell()
            line = source.readline()
            if not line or not line.endswith(b"\n"):
                source.seek(line_start)
                break
            offset = source.tell()
            if may_matter and not may_matter(line):
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            extracted = extractor(record)
            if extracted.session_id and not named:
                # A subagent's prompts come from its parent agent, not from
                # you. With no session id the file is tracked but never
                # indexed or listed (_refresh_sessions skips it).
                session_id, named = ("" if extracted.subagent else extracted.session_id), True
            if not session_id:
                continue
            cwd = cwd or extracted.cwd
            if extracted.title and (extracted.title_rank > title_rank or
                                    (extracted.title_rank == title_rank and title_rank > 1)):
                title, title_rank = extracted.title, extracted.title_rank
            for chunk in extracted.chunks:
                conn.execute(
                    "INSERT INTO entries(file_path, tool, session_id, ordinal, byte_start, byte_end, role, timestamp, text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(path), tool.value, session_id, ordinal, line_start, offset,
                     chunk.role.value, extracted.timestamp, chunk.text),
                )
            if extracted.chunks:
                ordinal += 1
    return offset, offset - start_offset, session_id, cwd, title, title_rank


def _apply_codex_names(conn: sqlite3.Connection, names: dict[str, str]) -> bool:
    before = conn.total_changes
    conn.executemany(
        "UPDATE files SET title=?, title_rank=4 WHERE tool=? AND session_id=? "
        "AND (title<>? OR title_rank<>4)",
        ((title, SearchTool.CODEX.value, session_id, title) for session_id, title in names.items()),
    )
    return conn.total_changes > before


def _refresh_sessions(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM sessions")
    rows = conn.execute(
        "SELECT * FROM files WHERE session_id != '' ORDER BY modified DESC, path DESC"
    )
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["tool"], row["session_id"])
        if key in seen:
            continue
        seen.add(key)
        cwd = row["cwd"]
        title = row["title"] or (Path(cwd).name if cwd else "") or "(untitled)"
        conn.execute(
            "INSERT INTO sessions(tool, session_id, path, cwd, title, mtime, size) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*key, row["path"], cwd, title, row["modified"], row["size"]),
        )
    return len(seen)


def _codex_id_from_name(path: Path) -> str:
    match = _CODEX_ID.search(path.stem)
    return match.group(1) if match else path.stem
