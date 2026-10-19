"""At-most-once delivery: sent actions are never replayed after a missing result."""

import uuid

from .conflict import Conflict
from .host_actions_protocol import ACTION_TTL, MAX_ACTIONS, MAX_HISTORY, PENDING, ActionKind, ActionState, action_body
from .host_protocol import OFFLINE_AFTER, session_id


def expire(data: dict, now: float) -> bool:
    changed = False
    for item in data.get("actions", []):
        if item["state"] in PENDING and now >= item["queued_at"] + ACTION_TTL:
            changed = True
            sent = item["state"] == ActionState.SENT
            item.update(state=ActionState.EXPIRED,
                        message="Result not received; outcome unknown. Check the Mac before retrying." if sent
                        else "Expired before delivery; nothing ran on the Mac.")

    return changed


def enqueue(data: dict, sid: str, kind: str, body: dict, now: float) -> dict:
    session_id(sid)
    expire(data, now)
    if now - data.get("last_seen", 0) >= OFFLINE_AFTER:
        raise Conflict("Mac is offline; wait for its next push before queueing an action.")
    source = data.get("trash", []) if kind == ActionKind.RESTORE else data.get("rows", [])
    row = next((r for r in source if r["id"] == sid), None)
    if row is None:
        raise LookupError("no Mac session with that id")
    clean = action_body(kind, row["tool"], body)
    if not row["mutable"]:
        raise Conflict("Mac Codex sessions are list-only until its app-server daemon is available.")
    items = data.setdefault("actions", [])
    pending = [i for i in items if i["state"] in PENDING]
    if any(i["session_id"] == sid for i in pending):
        raise Conflict("This Mac session already has an action waiting for a result.")
    if len(pending) >= MAX_ACTIONS:
        raise Conflict("Mac action queue is full; wait for results.")
    item = {"id": str(uuid.uuid4()), "session_id": sid, "tool": row["tool"], "action": kind,
            "body": clean, "cwd": row["cwd"], "title": row["title"], "queued_at": now,
            "state": ActionState.QUEUED, "message": ""}
    items.append(item)
    waiting = [i for i in items if i["state"] in PENDING]
    finished = [i for i in items if i["state"] not in PENDING]
    data["actions"] = sorted(finished[-(MAX_HISTORY - len(waiting)):] + waiting, key=lambda i: i["queued_at"])
    return item


def dispatch(data: dict, now: float) -> list[dict]:
    expire(data, now)
    out, uploading = [], False
    for item in data.get("actions", []):
        if item["state"] != ActionState.QUEUED:
            continue
        if item["action"] == ActionKind.FORK_PI:
            if uploading:
                continue
            uploading = True  # A single exchange may carry one capped transcript.
        out.append({key: item[key] for key in ("id", "session_id", "tool", "action", "body")}
                   | {"ttl": max(0, item["queued_at"] + ACTION_TTL - now)})
        if item["action"] == ActionKind.FORK_PI:
            out[-1]["source_cwd"] = item["cwd"]
        item["state"] = ActionState.SENT
    return out


def apply_results(data: dict, results: list[dict], uploads: list[dict], now: float, receive) -> list[str]:
    expire(data, now)
    by_id = {a["id"]: a for a in data.get("actions", [])}
    if len({r["id"] for r in results}) != len(results):
        raise ValueError("duplicate action result")
    # A result for an action this file no longer holds (pruned history, reset
    # state) is acknowledged and dropped: rejecting it would make the Mac resend
    # it forever, failing every later push.
    forgotten = [r["id"] for r in results if r["id"] not in by_id]
    results = [r for r in results if r["id"] in by_id]
    successful = {r["id"] for r in results if r["ok"]}
    uploaded = {}
    for upload in uploads:
        if not isinstance(upload, dict):
            raise ValueError("invalid upload")
        aid = session_id(upload.get("action_id"))
        item = by_id.get(aid)
        if not item or item["state"] != ActionState.SENT or item["action"] != ActionKind.FORK_PI:
            raise ValueError("unsolicited or expired transcript upload")
        if aid not in successful:
            raise ValueError("upload has no successful action result")
        if aid in uploaded or len(uploads) > 1:
            raise ValueError("only one transcript upload is allowed per exchange")
        uploaded[aid] = upload
    for aid in successful:
        item = by_id[aid]
        if item["state"] == ActionState.SENT and item["action"] == ActionKind.FORK_PI and aid not in uploaded:
            raise ValueError("fork onto Pi is missing its transcript")
    acknowledged = forgotten
    for result in results:
        item = by_id[result["id"]]
        acknowledged.append(item["id"])
        if item["state"] != ActionState.SENT:
            continue
        message, ok = result["message"], result["ok"]
        if item["action"] == ActionKind.FORK_PI and ok:
            if item["id"] not in uploaded:
                raise ValueError("fork onto Pi is missing its transcript")
            try:
                message = receive(item, uploaded.pop(item["id"]))
            except (OSError, ValueError, Conflict) as e:
                ok, message = False, " ".join(str(e).split())[:500]
        item.update(state=ActionState.DONE if ok else ActionState.FAILED, message=message)
    if uploaded:
        raise ValueError("upload has no successful action result")
    return acknowledged
