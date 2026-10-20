import json
from pathlib import Path

CLAUDE_ID = "11111111-1111-1111-1111-111111111111"
CLAUDE_ID_2 = "22222222-2222-2222-2222-222222222222"
CODEX_ID = "33333333-3333-3333-3333-333333333333"


def append_jsonl(path: Path, records, complete=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    if not complete:
        data = data.rstrip("\n")
    with path.open("a") as out:
        out.write(data)


def claude_path(root: Path, sid=CLAUDE_ID, project="project-a") -> Path:
    return root / "claude" / "projects" / project / f"{sid}.jsonl"


def codex_path(root: Path, sid=CODEX_ID) -> Path:
    return root / "codex" / "sessions" / "2026" / "10" / "18" / f"rollout-2026-10-18T00-00-00-{sid}.jsonl"


def claude_records(sid=CLAUDE_ID, cwd="/work/alpha"):
    return [
        {"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": "2026-10-10T10:00:00Z",
         "message": {"role": "user", "content": "amber prompt several words"}},
        {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": "2026-10-10T10:01:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "violet reasoning"},
             {"type": "text", "text": "cobalt reply with quoted phrase"},
             {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/ivory-tool-input"}},
         ]}},
        {"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": "2026-10-10T10:02:00Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "content": "scarlet tool output"},
         ]}},
        {"type": "attachment", "sessionId": sid, "cwd": cwd, "timestamp": "2026-10-10T10:03:00Z",
         "attachment": {"type": "queued_command", "prompt": "gold queued prompt"}},
        {"type": "custom-title", "sessionId": sid, "customTitle": "Alpha indexed session"},
    ]


def codex_records(sid=CODEX_ID, cwd="/work/codex"):
    """An older-format rollout: each message is both a response item and an event."""
    return [
        {"timestamp": "2026-10-11T10:00:00Z", "type": "session_meta",
         "payload": {"session_id": sid, "id": sid, "cwd": cwd, "source": "cli"}},
        {"timestamp": "2026-10-11T10:00:30Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "# AGENTS.md beige injected instructions"}]}},
        {"timestamp": "2026-10-11T10:01:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "ochre codex prompt"}]}},
        {"timestamp": "2026-10-11T10:01:00Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "ochre codex prompt", "images": []}},
        {"timestamp": "2026-10-11T10:01:30Z", "type": "response_item",
         "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "khaki codex reasoning"}],
                     "encrypted_content": "gAAAAB secretblobword"}},
        {"timestamp": "2026-10-11T10:02:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "teal codex reply"}]}},
        {"timestamp": "2026-10-11T10:02:00Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "teal codex reply"}},
        {"timestamp": "2026-10-11T10:02:30Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell", "arguments": "{\"cmd\": \"sepia tool input\"}"}},
        {"timestamp": "2026-10-11T10:03:00Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output",
                     "output": "umber codex tool output that prints \"agent_message\""}},
    ]


def codex_item_records(sid, cwd="/work/codex-new"):
    """A newer-format rollout: messages arrive as item_completed events."""
    def item(kind, text):
        return {"timestamp": "2026-10-12T10:00:00Z", "type": "event_msg",
                "payload": {"type": "item_completed", "thread_id": sid,
                            "item": {"type": kind, "content": [{"type": "text", "text": text}]}}}
    return [
        {"timestamp": "2026-10-12T10:00:00Z", "type": "session_meta",
         "payload": {"session_id": sid, "id": sid, "cwd": cwd, "source": "cli"}},
        item("UserMessage", "mauve item prompt"),
        item("AgentMessage", "cerise item reply"),
        item("CommandExecution", "plum command output"),
    ]
