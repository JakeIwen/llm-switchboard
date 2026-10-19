"""Extract searchable conversation text from Claude Code and Codex JSONL records."""

from datetime import datetime
from pathlib import Path
from typing import Iterator

from .search_records import ExtractedText, LineExtraction, SearchRole
from .transcripts import one_line

_CLAUDE_TITLE_RANK = {"first": 1, "summary": 2, "ai-title": 3, "custom-title": 4}


def extract_claude_record(record: dict) -> LineExtraction:
    """Mirror claudesearch.sh's conversation-text rules for one event."""
    if not isinstance(record, dict):
        return LineExtraction()
    record_type = record.get("type")
    chunks: list[ExtractedText] = []
    title = ""
    title_rank = 0
    if record_type == "user" and not record.get("isMeta"):
        chunks = list(_claude_content((record.get("message") or {}).get("content"), SearchRole.ME))
        first = _first_prompt(chunks)
        if first:
            title, title_rank = first, _CLAUDE_TITLE_RANK["first"]
    elif record_type == "assistant":
        chunks = list(_claude_content((record.get("message") or {}).get("content"), SearchRole.REPLY))
    elif record_type == "attachment" and (record.get("attachment") or {}).get("type") == "queued_command":
        chunks = list(_claude_content(record["attachment"].get("prompt"), SearchRole.ME))
    elif record_type == "custom-title":
        title, title_rank = str(record.get("customTitle") or ""), _CLAUDE_TITLE_RANK["custom-title"]
    elif record_type == "ai-title":
        title, title_rank = str(record.get("aiTitle") or ""), _CLAUDE_TITLE_RANK["ai-title"]
    elif record_type == "summary":
        title, title_rank = str(record.get("summary") or ""), _CLAUDE_TITLE_RANK["summary"]
    return LineExtraction(
        chunks=_combine_roles(chunks),
        session_id=str(record.get("sessionId") or ""),
        cwd=str(record.get("cwd") or ""),
        title=one_line(title),
        title_rank=title_rank,
        timestamp=parse_timestamp(record.get("timestamp")),
    )


def extract_codex_record(record: dict) -> LineExtraction:
    """Index every string, as codexsearch.sh does, while tagging its likely author."""
    if not isinstance(record, dict):
        return LineExtraction()
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    role = _codex_role(record)
    text = "\n".join(value for value in _all_strings(record) if value)
    session_id = cwd = ""
    if record.get("type") == "session_meta":
        session_id = str(payload.get("session_id") or payload.get("id") or "")
        cwd = str(payload.get("cwd") or "")
    return LineExtraction(
        chunks=(ExtractedText(role, text),) if text else (),
        session_id=session_id,
        cwd=cwd,
        timestamp=parse_timestamp(record.get("timestamp")),
    )


def codex_names(path: Path) -> dict[str, str]:
    """Last thread_name per id from Codex's append-only name index."""
    import json

    names: dict[str, str] = {}
    try:
        lines = path.open("rb")
    except OSError:
        return names
    with lines:
        for line in lines:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(record, dict) and record.get("id") and record.get("thread_name"):
                names[str(record["id"])] = one_line(str(record["thread_name"]))
    return names


def parse_timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _claude_content(value, default_role: SearchRole) -> Iterator[ExtractedText]:
    if isinstance(value, str):
        if value:
            yield ExtractedText(default_role, value)
        return
    if isinstance(value, list):
        for item in value:
            yield from _claude_content(item, default_role)
        return
    if not isinstance(value, dict):
        return
    kind = value.get("type")
    if kind == "text" and isinstance(value.get("text"), str):
        yield ExtractedText(default_role, value["text"])
    elif kind == "thinking" and isinstance(value.get("thinking"), str):
        yield ExtractedText(SearchRole.REPLY, value["thinking"])
    elif kind == "tool_use":
        for text in _all_strings(value.get("input")):
            yield ExtractedText(SearchRole.REPLY, text)
    elif kind == "tool_result":
        yield from _claude_content(value.get("content"), SearchRole.TOOL)


def _all_strings(value) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _all_strings(item)


def _codex_role(record: dict) -> SearchRole:
    record_type = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    if record_type == "response_item" and payload_type == "message":
        role = payload.get("role")
        # Codex repeats injected project instructions as role=user, but actual
        # prompts carry response metadata (and also appear as UserMessage events).
        if role == "user" and record.get("metadata") is not None:
            return SearchRole.ME
        if role == "assistant":
            return SearchRole.REPLY
    if payload_type in ("reasoning", "agent_message"):
        return SearchRole.REPLY
    if payload_type in ("custom_tool_call", "custom_tool_call_output", "function_call", "function_call_output"):
        return SearchRole.TOOL
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    item_type = str(item.get("type") or "").lower()
    if item_type == "usermessage":
        return SearchRole.ME
    if item_type in ("agentmessage", "reasoning"):
        return SearchRole.REPLY
    if item_type and item_type not in ("plan",):
        return SearchRole.TOOL
    return SearchRole.OTHER


def _combine_roles(chunks: list[ExtractedText]) -> tuple[ExtractedText, ...]:
    grouped: dict[SearchRole, list[str]] = {}
    for chunk in chunks:
        if chunk.text:
            grouped.setdefault(chunk.role, []).append(chunk.text)
    return tuple(ExtractedText(role, "\n".join(texts)) for role, texts in grouped.items())


def _first_prompt(chunks: list[ExtractedText]) -> str:
    text = " ".join(chunk.text for chunk in chunks if chunk.role == SearchRole.ME).strip()
    return "" if text.startswith("<") else text
