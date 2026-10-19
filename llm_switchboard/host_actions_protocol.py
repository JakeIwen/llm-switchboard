"""The action allow-list and cross-process records, checked on both machines."""

from enum import StrEnum
from typing import NotRequired, TypedDict

from .host_protocol import HostRow, Push, folder, number, row, session_id, text
from .names import clean_title

ACTION_TTL = 60
MAX_ACTIONS = 20
MAX_HISTORY = 200
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_EXCHANGE_BYTES = 4 * 1024 * 1024 + ((MAX_UPLOAD_BYTES + 2) // 3) * 4


class ActionKind(StrEnum):
    RENAME = "rename"
    DELETE = "delete"
    RESTORE = "restore"
    FORK = "fork"
    MOVE = "move"
    FORK_PI = "fork-pi"


class ActionState(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


PENDING = {ActionState.QUEUED, ActionState.SENT}


class HostAction(TypedDict):
    id: str
    session_id: str
    tool: str
    action: str
    body: dict
    ttl: float
    source_cwd: NotRequired[str]


class HostResult(TypedDict):
    id: str
    ok: bool
    message: str


class Upload(TypedDict):
    action_id: str
    transcript: str  # base64; accepted only for a pending fork-pi


class ActionReply(TypedDict):
    version: int
    actions: list[HostAction]
    acknowledged: list[str]


class TrashRow(HostRow):
    deleted_at: float


class ActionPush(Push):
    trash: list[TrashRow]
    results: list[HostResult]
    uploads: list[Upload]
    ready: bool


def action_body(action: str, tool: str, body) -> dict:
    if not isinstance(action, str) or action not in set(ActionKind):
        raise ValueError("action is not allowed on the Mac")
    if tool not in ("claude", "codex"):
        raise ValueError("unknown tool")
    if tool == "codex" and action in (ActionKind.MOVE, ActionKind.FORK_PI):
        raise ValueError("Codex cannot move or fork onto the Pi; fork it on the Mac instead")
    if not isinstance(body, dict):
        raise ValueError("action body must be an object")
    fields = {ActionKind.RENAME: {"title"}, ActionKind.FORK: {"title", "cwd"},
              ActionKind.MOVE: {"cwd"}, ActionKind.FORK_PI: {"title"}}.get(action, set())
    if set(body) - fields:
        raise ValueError("unexpected action fields (keep, prompts and commands are never accepted)")
    clean = {}
    if "title" in fields:
        clean["title"] = clean_title(text(body.get("title"), "title", 120))
    if "cwd" in fields:
        clean["cwd"] = folder(body.get("cwd"))
    return clean


def action(raw) -> HostAction:
    if not isinstance(raw, dict):
        raise ValueError("action must be an object")
    aid, sid = session_id(raw.get("id")), session_id(raw.get("session_id"))
    kind, tool = raw.get("action"), raw.get("tool")
    body = action_body(kind, tool, raw.get("body"))
    ttl = number(raw.get("ttl"), "ttl")
    if ttl is None or ttl <= 0 or ttl > ACTION_TTL:
        raise ValueError("action expired or invalid remaining lifetime")
    clean = HostAction(id=aid, session_id=sid, tool=tool, action=kind, body=body, ttl=ttl)
    if kind == ActionKind.FORK_PI:
        clean["source_cwd"] = folder(raw.get("source_cwd"))
    return clean


def results(raw) -> list[HostResult]:
    if not isinstance(raw, list) or len(raw) > MAX_HISTORY:
        raise ValueError("too many action results")
    clean = []
    for result in raw:
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("invalid action result")
        clean.append(HostResult(id=session_id(result.get("id")), ok=result["ok"],
                                message=text(result.get("message", ""), "result message", 500, empty=True)))
    return clean


def trash_items(raw) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > 5000:
        raise ValueError("too many trash items")
    clean = []
    for item in raw:
        r = row(item)
        deleted = number(item.get("deleted_at"), "deleted_at")
        if deleted is None:
            raise ValueError("missing trash deletion time")
        clean.append({**r, "deleted_at": deleted})
    return clean
