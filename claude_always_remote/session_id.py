"""Claude Code session ids are lowercase UUIDs; anything else is rejected."""

import re

_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def is_session_id(value: str) -> bool:
    return bool(_SESSION_ID_RE.match(value))


def short_id(session_id: str) -> str:
    return session_id[:8]
