"""Extract your prompts and the visible replies from Claude Code and Codex JSONL.

Thinking, tool calls, tool results and injected context are left out: they
are most of the bytes, and nobody searches them for a conversation.
"""

from datetime import datetime
from pathlib import Path

from .codex_threads import is_subagent_meta
from .search_records import ExtractedText, LineExtraction, SearchRole
from .transcripts import one_line, prompt_text, reply_text

_CLAUDE_TITLE_RANK = {"first": 1, "summary": 2, "ai-title": 3, "custom-title": 4}

# Every Codex line extract_codex_record uses holds one of these, as JSON
# strings; checking the raw bytes first spares parsing the large tool output.
_CODEX_MARKERS = (b'"session_meta"', b'"user_message"', b'"agent_message"',
                  b'"UserMessage"', b'"AgentMessage"')
_CODEX_ITEM_ROLES = {"UserMessage": SearchRole.ME, "AgentMessage": SearchRole.REPLY}


def extract_claude_record(record: dict) -> LineExtraction:
    if not isinstance(record, dict):
        return LineExtraction()
    record_type = record.get("type")
    chunks: tuple[ExtractedText, ...] = ()
    title = ""
    title_rank = 0
    if record_type == "user":
        prompt = prompt_text(record)
        if prompt:
            chunks = (ExtractedText(SearchRole.ME, prompt),)
            title, title_rank = prompt, _CLAUDE_TITLE_RANK["first"]
    elif record_type == "assistant":
        reply = reply_text(record)
        chunks = (ExtractedText(SearchRole.REPLY, reply),) if reply else ()
    elif record_type == "attachment" and (record.get("attachment") or {}).get("type") == "queued_command":
        prompt = _text_blocks(record["attachment"].get("prompt"))
        chunks = (ExtractedText(SearchRole.ME, prompt),) if prompt else ()
    elif record_type == "custom-title":
        title, title_rank = str(record.get("customTitle") or ""), _CLAUDE_TITLE_RANK["custom-title"]
    elif record_type == "ai-title":
        title, title_rank = str(record.get("aiTitle") or ""), _CLAUDE_TITLE_RANK["ai-title"]
    elif record_type == "summary":
        title, title_rank = str(record.get("summary") or ""), _CLAUDE_TITLE_RANK["summary"]
    # No session_id: the file name is the session, as in claudesearch.sh.
    return LineExtraction(
        chunks=chunks,
        cwd=str(record.get("cwd") or ""),
        title=one_line(title),
        title_rank=title_rank,
        timestamp=parse_timestamp(record.get("timestamp")),
    )


def codex_line_may_matter(line: bytes) -> bool:
    return any(marker in line for marker in _CODEX_MARKERS)


def extract_codex_record(record: dict) -> LineExtraction:
    """Prompts and replies come from Codex's event stream. Its response items
    repeat them among reasoning, tool calls and injected instructions, so
    reading both would index every message twice."""
    if not isinstance(record, dict):
        return LineExtraction()
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if record.get("type") == "session_meta":
        return LineExtraction(
            session_id=str(payload.get("session_id") or payload.get("id") or ""),
            cwd=str(payload.get("cwd") or ""),
            subagent=is_subagent_meta(payload),
        )
    if record.get("type") != "event_msg":
        return LineExtraction()
    message = _codex_message(payload)
    return LineExtraction(
        chunks=(message,) if message and message.text else (),
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


def codex_first_prompt(path: Path) -> str:
    """Your first prompt in a Codex rollout, one line, or "" when it has none."""
    import json

    try:
        lines = path.open("rb")
    except OSError:
        return ""
    with lines:
        for line in lines:
            if not codex_line_may_matter(line):
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            for chunk in extract_codex_record(record).chunks:
                if chunk.role == SearchRole.ME and chunk.text:
                    return one_line(chunk.text)
    return ""


def parse_timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _codex_message(payload: dict) -> ExtractedText | None:
    # Older rollouts write user_message/agent_message events, newer ones
    # item_completed; a rollout never has both.
    kind = payload.get("type")
    if kind == "user_message":
        return ExtractedText(SearchRole.ME, str(payload.get("message") or "").strip())
    if kind == "agent_message":
        return ExtractedText(SearchRole.REPLY, str(payload.get("message") or "").strip())
    item = payload.get("item") if kind == "item_completed" else None
    role = _CODEX_ITEM_ROLES.get(item.get("type")) if isinstance(item, dict) else None
    return ExtractedText(role, _text_blocks(item.get("content"))) if role else None


def _text_blocks(value) -> str:
    """A plain string, or the text of a list of {type, text} blocks."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    return "\n".join(str(b.get("text") or "") for b in value
                     if isinstance(b, dict) and isinstance(b.get("text"), str)).strip()
