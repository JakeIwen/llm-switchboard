"""The last few messages of a conversation, for a quick look in the picker."""

from pathlib import Path
import json

from .transcripts import prompt_text

# Enough of the file's end to hold several recent turns, even with large tool output.
_TAIL_BYTES = 768 * 1024
_TEXT_LEN = 800


def recent_messages(path: Path, limit: int = 8) -> list[dict]:
    """[{role, text, at}] for the last `limit` prompts and replies, oldest first.
    Tool calls, tool results and thinking are left out."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - _TAIL_BYTES))
        data = f.read()
    lines = data.split(b"\n")
    if size > _TAIL_BYTES:
        lines = lines[1:]  # the first line was cut by the seek
    messages = []
    for line in lines:
        if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        text = _text(record)
        if text:
            messages.append({"role": record["type"], "text": clip_message(text), "at": record.get("timestamp", "")})
    return messages[-limit:]


def _text(record: dict) -> str:
    if record.get("type") == "user":
        return prompt_text(record)
    if record.get("type") != "assistant":
        return ""
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()


def clip_message(text: str) -> str:
    return text if len(text) <= _TEXT_LEN else text[: _TEXT_LEN - 1] + "…"
