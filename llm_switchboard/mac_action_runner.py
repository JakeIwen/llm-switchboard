"""Durable Mac-side delivery ledger: never repeat an action after a crash."""

import time

from .host_actions_protocol import MAX_ACTIONS, action
from .host_protocol import session_id
from .json_file import JsonFile
from .paths import STATE_DIR


class MacActionRunner:
    def __init__(self, executor, state=STATE_DIR / "host-actions.json", clock=time.monotonic, wall=time.time):
        self.executor, self.file, self.clock, self.wall = executor, JsonFile(state), clock, wall

    def pending(self) -> list[dict]:
        return [item["result"] for item in self.file.load().get("actions", {}).values() if not item.get("ack")]

    def acknowledge(self, ids: list) -> None:
        if not isinstance(ids, list):
            raise ValueError("invalid action acknowledgements")
        if not ids:
            return
        with self.file.locked():
            data = self.file.load()
            records = data.setdefault("actions", {})
            for aid in ids:
                if session_id(aid) in records:
                    records[aid]["ack"] = True
            self.file.write(data)

    def run(self, items: list, transport_elapsed=0.0) -> tuple[list[dict], list[dict]]:
        if not isinstance(items, list) or len(items) > MAX_ACTIONS:
            raise ValueError("invalid action batch")
        started, wall_started = self.clock(), self.wall()
        results, uploads = [], []
        for raw in items:
            aid = session_id(raw.get("id") if isinstance(raw, dict) else None)
            elapsed = transport_elapsed + max(self.clock() - started, self.wall() - wall_started, 0)
            result, upload = self._run_one(aid, raw, elapsed)
            results.append(result)
            if upload:
                uploads.append(upload)
        return results, uploads

    def _run_one(self, aid, raw, elapsed):
        started, wall_started = self.clock(), self.wall()
        with self.file.locked():
            data = self.file.load()
            records = data.setdefault("actions", {})
            if aid in records:
                return records[aid]["result"], None
            # Persist before the edit. A crash has an unknown outcome, never an automatic retry.
            unknown = {"id": aid, "ok": False, "message": "Mac action interrupted; outcome unknown. Check before retrying."}
            records[aid] = {"result": unknown, "ack": False}
            self.file.write(data)
            upload = None
            try:
                item = action(raw)
                elapsed += max(self.clock() - started, self.wall() - wall_started, 0)
                if elapsed >= item["ttl"]:
                    raise ValueError("Action expired before execution on the Mac.")
                outcome, upload = self.executor.execute(item)
                result = {"id": aid, "ok": True, "message": "Done on the Mac."}
                if outcome.get("id"):
                    result["message"] = f"Forked on the Mac as {outcome['id']}; not kept running."
            except Exception as e:
                result = {"id": aid, "ok": False, "message": " ".join(str(e).split())[:500]}
            # Upload bytes are never persisted. If their reply is lost, report uncertainty, not a second export.
            records[aid]["result"] = unknown if upload else result
            self.file.write(data)
            return result, upload
