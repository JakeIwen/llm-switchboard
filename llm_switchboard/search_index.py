"""SQLite FTS5 schema and read-side transcript search."""

from pathlib import Path
import sqlite3
import time

from .codex_threads import codex_short_id
from .launcher import codex_resume_command, resume_command
from .search_query import SearchQuery, fts_literal
from .search_records import IndexState, SearchContext, SearchDetail, SearchResult, SearchStatus, SearchTool
from .session_id import short_id
from .transcripts import display_path

SCHEMA_VERSION = 1
_QUERY_TIMEOUT_S = 5.0


class SearchTimeout(RuntimeError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    session_id TEXT NOT NULL,
    cwd TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    title_rank INTEGER NOT NULL DEFAULT 0,
    inode INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    modified REAL NOT NULL,
    byte_offset INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    role TEXT NOT NULL,
    timestamp REAL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_session ON entries(tool, session_id);
CREATE INDEX IF NOT EXISTS entries_context ON entries(file_path, ordinal);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    text, content='entries', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS sessions (
    tool TEXT NOT NULL,
    session_id TEXT NOT NULL,
    path TEXT NOT NULL,
    cwd TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    mtime REAL,
    size INTEGER,
    PRIMARY KEY(tool, session_id)
);
CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE OF text ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def open_writer(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA wal_autocheckpoint=4096")
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise RuntimeError(f"search index schema {version} is not supported")
    conn.executescript(_SCHEMA)
    create_fts_triggers(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn


def create_fts_triggers(conn: sqlite3.Connection) -> None:
    conn.executescript(_TRIGGERS)


def drop_fts_triggers(conn: sqlite3.Connection) -> None:
    conn.executescript("DROP TRIGGER IF EXISTS entries_ai; DROP TRIGGER IF EXISTS entries_ad; "
                       "DROP TRIGGER IF EXISTS entries_au;")


def rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('rebuild')")
    conn.commit()
    create_fts_triggers(conn)


def write_status(conn: sqlite3.Connection, state: IndexState, *, sessions: int | None = None,
                 files: int | None = None, started_at: float | None = None,
                 last_updated: float | None = None, error: str = "") -> None:
    values = {"state": state.value, "error": error}
    for key, value in (("sessions", sessions), ("files", files), ("started_at", started_at),
                       ("last_updated", last_updated)):
        if value is not None:
            values[key] = str(value)
    conn.executemany(
        "INSERT INTO index_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        values.items(),
    )
    conn.commit()


class TranscriptSearch:
    def __init__(self, path: Path):
        self.path = Path(path)

    def status(self) -> SearchStatus:
        if not self.path.is_file():
            return _missing_status()
        try:
            with self._reader() as conn:
                meta = dict(conn.execute("SELECT key, value FROM index_meta"))
                sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
                files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
        except sqlite3.Error as exc:
            return {**_missing_status(), "state": IndexState.ERROR.value, "error": str(exc)}
        return {
            "state": meta.get("state", IndexState.MISSING.value),
            "sessions": int(meta.get("sessions", sessions)),
            "files": int(meta.get("files", files)),
            "last_updated": _float_or_none(meta.get("last_updated")),
            "started_at": _float_or_none(meta.get("started_at")),
            "error": meta.get("error", ""),
        }

    def search(self, query: SearchQuery, limit: int = 50) -> list[SearchResult]:
        if not self.path.is_file():
            return []
        try:
            with self._reader() as conn:
                rows = self._summary_rows(conn, query, limit)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise SearchTimeout("query took too long; add a more specific word or phrase") from exc
            return []
        return [_result(row, query.positive_terms) for row in rows]

    def detail(self, query: SearchQuery, tool: str, session_id: str) -> SearchDetail | None:
        if tool not in {item.value for item in SearchTool} or not self.path.is_file():
            return None
        try:
            with self._reader() as conn:
                summary = self._summary_rows(conn, query, 1, tool, session_id)
                if not summary:
                    return None
                matches = self._matching_rows(conn, query, 12, tool, session_id)
                contexts = [_context(conn, row, query.positive_terms) for row in matches]
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise SearchTimeout("query took too long; add a more specific word or phrase") from exc
            return None
        return {"result": _result(summary[0], query.positive_terms), "contexts": contexts}

    def _reader(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + _QUERY_TIMEOUT_S
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10_000)
        return conn

    def _summary_rows(self, conn: sqlite3.Connection, query: SearchQuery, limit: int,
                      tool: str = "", session_id: str = "") -> list[sqlite3.Row]:
        where, params = _match_where(query, tool, session_id)
        sql = f"""
            WITH matched AS (
                SELECT e.id, e.file_path, e.ordinal, e.role, e.timestamp, e.text,
                       s.tool, s.session_id, s.cwd, s.title, s.mtime, s.size,
                       COALESCE(e.timestamp, s.mtime) AS matched_at
                  FROM entries_fts
                  JOIN entries e ON e.id=entries_fts.rowid
                  JOIN sessions s ON s.tool=e.tool AND s.session_id=e.session_id
                 WHERE {where}
            ), ranked AS (
                SELECT *, count(*) OVER (PARTITION BY tool, session_id) AS match_count,
                       row_number() OVER (
                           PARTITION BY tool, session_id
                           ORDER BY matched_at DESC, id DESC
                       ) AS rank
                  FROM matched
            )
            SELECT * FROM ranked WHERE rank=1
             ORDER BY matched_at DESC, mtime DESC LIMIT ?
        """
        return list(conn.execute(sql, [*params, max(1, min(limit, 200))]))

    def _matching_rows(self, conn: sqlite3.Connection, query: SearchQuery, limit: int,
                       tool: str, session_id: str) -> list[sqlite3.Row]:
        where, params = _match_where(query, tool, session_id)
        sql = f"""
            SELECT e.id, e.file_path, e.ordinal, e.role, e.timestamp, e.text,
                   s.tool, s.session_id, s.cwd, s.title, s.mtime, s.size,
                   COALESCE(e.timestamp, s.mtime) AS matched_at
              FROM entries_fts
              JOIN entries e ON e.id=entries_fts.rowid
              JOIN sessions s ON s.tool=e.tool AND s.session_id=e.session_id
             WHERE {where}
             ORDER BY matched_at DESC, e.id DESC LIMIT ?
        """
        return list(conn.execute(sql, [*params, limit]))


def _match_where(query: SearchQuery, tool: str, session_id: str) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    positive = query.positive_terms
    match = " AND ".join(fts_literal(term) for term in positive)
    clauses.append("entries_fts MATCH ?")
    params.append(match)
    _add_session_filters(query, clauses, params)
    _add_role_filter(query, "e", clauses, params)
    if tool:
        clauses.extend(("s.tool=?", "s.session_id=?"))
        params.extend((tool, session_id))
    for term in query.negative_terms:
        excluded, excluded_params = _term_sessions(query, term)
        clauses.append("(e.tool, e.session_id) NOT IN (" + excluded + ")")
        params.extend(excluded_params)
    return " AND ".join(clauses), params


def _add_session_filters(query: SearchQuery, clauses: list[str], params: list) -> None:
    include_tools = tuple(dict.fromkeys(f.value for f in query.values if f.field == "tool" and not f.negative))
    exclude_tools = tuple(dict.fromkeys(f.value for f in query.values if f.field == "tool" and f.negative))
    if include_tools:
        clauses.append(f"s.tool IN ({','.join('?' for _ in include_tools)})")
        params.extend(include_tools)
    if exclude_tools:
        clauses.append(f"s.tool NOT IN ({','.join('?' for _ in exclude_tools)})")
        params.extend(exclude_tools)
    for item in (f for f in query.values if f.field == "dir"):
        expression = "lower(s.cwd) NOT LIKE ?" if item.negative else "lower(s.cwd) LIKE ?"
        clauses.append(expression)
        params.append(f"%{_escape_like(item.value.casefold())}%")
        clauses[-1] += " ESCAPE '\\'"
    for item in query.dates:
        expression = f"s.mtime {item.operator} ?"
        clauses.append(f"NOT ({expression})" if item.negative else expression)
        params.append(item.timestamp)


def _add_role_filter(query: SearchQuery, alias: str, clauses: list[str], params: list) -> None:
    include = tuple(dict.fromkeys(f.value for f in query.values if f.field == "by" and not f.negative))
    exclude = tuple(dict.fromkeys(f.value for f in query.values if f.field == "by" and f.negative))
    if include:
        clauses.append(f"{alias}.role IN ({','.join('?' for _ in include)})")
        params.extend(include)
    if exclude:
        clauses.append(f"{alias}.role NOT IN ({','.join('?' for _ in exclude)})")
        params.extend(exclude)


def _term_sessions(query: SearchQuery, term) -> tuple[str, list]:
    clauses = ["entries_fts MATCH ?"]
    params: list = [fts_literal(term)]
    _add_role_filter(query, "rx", clauses, params)
    sql = ("SELECT rx.tool, rx.session_id FROM entries_fts "
           "JOIN entries rx ON rx.id=entries_fts.rowid WHERE " + " AND ".join(clauses))
    return sql, params


def _result(row: sqlite3.Row, terms=()) -> SearchResult:
    tool = row["tool"]
    session_id = row["session_id"]
    cwd = row["cwd"] or ""
    command = codex_resume_command(session_id, cwd) if tool == SearchTool.CODEX.value else resume_command(session_id, cwd)
    return {
        "tool": tool,
        "id": session_id,
        "short_id": codex_short_id(session_id) if tool == SearchTool.CODEX.value else short_id(session_id),
        "title": row["title"] or Path(cwd).name or "(untitled)",
        "cwd": cwd,
        "project": display_path(cwd),
        "mtime": row["mtime"],
        "count": row["match_count"] if "match_count" in row.keys() else 1,
        "matched_at": row["matched_at"],
        "excerpt": excerpt(row["text"], terms),
        "resume_cmd": command,
        "host": "",
        "title_only": False,
    }


def excerpt(text: str, terms=(), radius: int = 90) -> str:
    flat = " ".join(str(text).split())
    folded = flat.casefold()
    positions = [folded.find((term.text if hasattr(term, "text") else str(term)).casefold()) for term in terms]
    positions = [pos for pos in positions if pos >= 0]
    at = min(positions) if positions else 0
    start, end = max(0, at - radius), min(len(flat), at + radius + 90)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def _context(conn: sqlite3.Connection, match: sqlite3.Row, terms=()) -> SearchContext:
    rows = conn.execute(
        "SELECT id, role, timestamp, text FROM entries "
        "WHERE file_path=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal, id",
        (match["file_path"], max(0, match["ordinal"] - 1), match["ordinal"] + 1),
    )
    return {
        "matched_at": match["matched_at"],
        "lines": [{"role": row["role"], "text": excerpt(row["text"], terms if row["id"] == match["id"] else (), radius=180),
                   "at": row["timestamp"], "match": row["id"] == match["id"]} for row in rows],
    }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _float_or_none(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _missing_status() -> SearchStatus:
    return {"state": IndexState.MISSING.value, "sessions": 0, "files": 0,
            "last_updated": None, "started_at": None, "error": ""}
