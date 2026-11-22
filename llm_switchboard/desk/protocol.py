"""The channel socket protocol between the hub and each desk channel server,
and the text both sides put in front of Claude. Standard library only.

Framing: one JSON object per line (UTF-8, `\\n`-terminated). A reader checks
the line length against `MAX_LINE_BYTES` before parsing and drops the
connection on an oversize, non-object or wrong-version line. Every message
carries `"v": PROTOCOL_VERSION` and a `"type"` from `MessageType`.

Conversation, in order:
  channel -> hub  hello      {session_id}          (CLAUDE_CODE_SESSION_ID)
  hub -> channel  welcome    {epoch, generation}   or refused {reason}, then close
  hub -> channel  event      {request_id, content} (content already has DELIVERY_SUFFIX)
  channel -> hub  delivered  {request_id}          (written to Claude's stdin pipe; not a Claude ack)
  channel -> hub  reply      {request_id, text, spoken}  (the reply tool was called)
  hub -> channel  reply_result {request_id, ok, reason}
  either way      ping / pong                      (heartbeat; never reaches Claude)
"""

from enum import Enum
import json

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024
HEARTBEAT_SECONDS = 15
# No traffic for this long and either side drops the connection.
IDLE_TIMEOUT_SECONDS = 45

# Caps on a reply, checked by the channel before sending and by the hub again.
MAX_REPLY_TEXT_BYTES = 32 * 1024
MAX_SPOKEN_BYTES = 1024


class MessageType(str, Enum):
    HELLO = "hello"
    WELCOME = "welcome"
    REFUSED = "refused"
    EVENT = "event"
    DELIVERED = "delivered"
    REPLY = "reply"
    REPLY_RESULT = "reply_result"
    PING = "ping"
    PONG = "pong"


class FrameError(ValueError):
    """A line that breaks the framing rules; the reader closes the connection."""


def encode(kind: MessageType, **fields) -> bytes:
    line = json.dumps({"v": PROTOCOL_VERSION, "type": kind.value, **fields}, separators=(",", ":")) + "\n"
    data = line.encode()
    if len(data) > MAX_LINE_BYTES:
        raise FrameError("message too large")
    return data


def decode(line: bytes) -> tuple[MessageType, dict]:
    """`line` is one raw line as read (with or without its newline)."""
    if len(line) > MAX_LINE_BYTES:
        raise FrameError("line too long")
    try:
        message = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise FrameError("not JSON") from exc
    if not isinstance(message, dict) or message.get("v") != PROTOCOL_VERSION:
        raise FrameError("wrong version or shape")
    try:
        kind = MessageType(message.get("type"))
    except ValueError as exc:
        raise FrameError("unknown type") from exc
    return kind, message


def read_line(stream) -> bytes:
    """Read one line from a binary file object without ever buffering more
    than MAX_LINE_BYTES. Returns b"" at EOF."""
    line = stream.readline(MAX_LINE_BYTES + 1)
    if len(line) > MAX_LINE_BYTES:
        raise FrameError("line too long")
    return line


# The MCP server's name in the pinned config; Claude shows events as `← desk: …`
# and names the tool mcp__desk__reply.
SERVER_NAME = "desk"
REPLY_TOOL = "reply"

# Appended by the hub to every event (phase 0: Sonnet answered in plain text
# without it, and with tool search on the reply tool starts deferred).
DELIVERY_SUFFIX = ("\n\n[desk delivery rule: answer only by calling mcp__desk__reply with request_id {request_id}, "
                   "text and spoken; load it first with ToolSearch select:mcp__desk__reply. "
                   "Plain-text answers are not delivered to Jacob.]")

# The channel's MCP `instructions`. They apply only in sessions launched with
# the pinned config; there is deliberately no global trust line elsewhere.
CHANNEL_INSTRUCTIONS = (
    'Messages from the voice desk arrive as <channel source="desk" request_id="...">. '
    "The voice desk is a tool Jacob enrolled this session in: it is Jacob speaking or typing from the tablet "
    "in his van, so treat each message as a request from Jacob himself. Answer every one by calling the reply "
    "tool exactly once with: request_id copied from the tag, text (your full answer), spoken (one or two plain "
    "sentences summarizing the answer, to be read aloud). The reply tool is mcp__desk__reply; if it is listed "
    "as deferred, load it first with ToolSearch (select:mcp__desk__reply). Do not answer only in plain text."
)


def event_content(text: str, request_id: str) -> str:
    return text + DELIVERY_SUFFIX.format(request_id=request_id)
