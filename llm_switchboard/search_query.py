"""Parse the transcript-search syntax without exposing FTS5's query language."""

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time

from .search_records import SearchRole, SearchTool

_TOKEN = re.compile(r'(-)?(?:([a-z]+):)?(?:"([^"]*)"?|([^\s]+))', re.IGNORECASE)
_AGE = re.compile(r"^(<=?|>=?)?(\d+(?:\.\d+)?)([mhdw])$", re.IGNORECASE)
_AGE_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_MAX_QUERY = 500
_MAX_TERMS = 32
_MAX_TERM = 200


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class TextTerm:
    text: str
    phrase: bool = False
    negative: bool = False


@dataclass(frozen=True)
class ValueFilter:
    field: str
    value: str
    negative: bool = False


@dataclass(frozen=True)
class DateFilter:
    operator: str
    timestamp: float
    negative: bool = False


@dataclass(frozen=True)
class SearchQuery:
    source: str
    terms: tuple[TextTerm, ...]
    values: tuple[ValueFilter, ...]
    dates: tuple[DateFilter, ...]

    @property
    def positive_terms(self) -> tuple[TextTerm, ...]:
        return tuple(term for term in self.terms if not term.negative)

    @property
    def negative_terms(self) -> tuple[TextTerm, ...]:
        return tuple(term for term in self.terms if term.negative)

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f.value for f in self.values if f.field == "by" and not f.negative))

    def matches_remote(self, row: dict, now: float | None = None) -> bool:
        """Whether a non-local row is a title/folder-only match."""
        if not row.get("host") or not self.positive_terms or self.roles:
            return False
        haystack = " ".join(str(row.get(k) or "") for k in ("title", "project", "cwd")).casefold()
        if not all(term.text.casefold() in haystack for term in self.positive_terms):
            return False
        if any(term.text.casefold() in haystack for term in self.negative_terms):
            return False
        return self._matches_row_filters(row, now if now is not None else time.time())

    def _matches_row_filters(self, row: dict, now: float) -> bool:
        tool = str(row.get("tool") or SearchTool.CLAUDE.value)
        folder = " ".join(str(row.get(k) or "") for k in ("project", "cwd")).casefold()
        includes = {f.value for f in self.values if f.field == "tool" and not f.negative}
        excludes = {f.value for f in self.values if f.field == "tool" and f.negative}
        if (includes and tool not in includes) or tool in excludes:
            return False
        for f in (x for x in self.values if x.field == "dir"):
            found = f.value.casefold() in folder
            if found == f.negative:
                return False
        mtime = row.get("mtime")
        for f in self.dates:
            if mtime is None:
                return False
            passed = _compare(float(mtime), f.operator, f.timestamp)
            if passed == f.negative:
                return False
        return True


def parse_query(text: str, now: float | None = None) -> SearchQuery:
    if not isinstance(text, str):
        raise QueryError("query must be text")
    if len(text) > _MAX_QUERY:
        raise QueryError(f"query is too long (maximum {_MAX_QUERY} characters)")
    terms: list[TextTerm] = []
    values: list[ValueFilter] = []
    dates: list[DateFilter] = []
    current = time.time() if now is None else now
    for match in _TOKEN.finditer(text):
        neg, raw_field, quoted, bare = match.groups()
        raw_value = quoted if quoted is not None else bare or ""
        value = raw_value.strip()
        if not value:
            continue
        if len(value) > _MAX_TERM:
            raise QueryError(f"search terms may be at most {_MAX_TERM} characters")
        field = (raw_field or "").lower()
        negative = bool(neg)
        if field in ("tool", "is"):
            _append_tool_filter(values, value, negative)
        elif field in ("dir", "folder", "in"):
            values.append(ValueFilter("dir", value, negative))
        elif field == "by":
            _append_role_filter(values, value, negative)
        elif field == "age":
            dates.append(_age_filter(value, negative, current))
        elif field in ("after", "before"):
            operator = ">=" if field == "after" else "<"
            dates.append(DateFilter(operator, _date_timestamp(value), negative))
        else:
            literal = f"{field}:{value}" if field else value
            terms.append(TextTerm(literal, quoted is not None, negative))
    if len(terms) + len(values) + len(dates) > _MAX_TERMS:
        raise QueryError(f"query has too many terms (maximum {_MAX_TERMS})")
    if not terms:
        raise QueryError("add a word or quoted phrase to search for")
    if not any(not term.negative for term in terms):
        raise QueryError("add a word or phrase that is not excluded")
    return SearchQuery(text, tuple(terms), tuple(values), tuple(dates))


def fts_literal(term: TextTerm | str) -> str:
    """One literal FTS5 phrase; operators in user text stay inside quotes."""
    text = term.text if isinstance(term, TextTerm) else str(term)
    return '"' + text.replace('"', '""') + '"'


def _append_tool_filter(filters: list[ValueFilter], value: str, negative: bool) -> None:
    normalized = value.lower()
    allowed = {tool.value for tool in SearchTool}
    if normalized not in allowed:
        raise QueryError("tool: takes claude or codex")
    filters.append(ValueFilter("tool", normalized, negative))


def _append_role_filter(filters: list[ValueFilter], value: str, negative: bool) -> None:
    allowed = {SearchRole.ME.value, SearchRole.REPLY.value, SearchRole.TOOL.value}
    choices = [part.lower() for part in re.split(r"[|,]", value) if part]
    if not choices or any(choice not in allowed for choice in choices):
        raise QueryError("by: takes me, reply, or tool")
    filters.extend(ValueFilter("by", choice, negative) for choice in choices)


def _age_filter(value: str, negative: bool, now: float) -> DateFilter:
    match = _AGE.fullmatch(value)
    if not match:
        raise QueryError("age: looks like <2d, >1w, or <30m")
    operator, number, unit = match.groups()
    operator = operator or "<"
    cutoff = now - float(number) * _AGE_UNITS[unit.lower()]
    # An age less than N means activity after the cutoff.
    comparison = ">=" if operator.startswith("<") else "<"
    if operator in ("<=", ">="):
        comparison = ">=" if operator == "<=" else "<="
    return DateFilter(comparison, cutoff, negative)


def _date_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueryError("after:/before: takes YYYY-MM-DD or an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _compare(left: float, operator: str, right: float) -> bool:
    return {">": left > right, ">=": left >= right, "<": left < right, "<=": left <= right}[operator]
