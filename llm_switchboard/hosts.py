"""Host snapshots, received through SSH and read by the web service."""

from pathlib import Path
import json
import shlex
import time

from .host_protocol import MAX_METADATA_BYTES, OFFLINE_AFTER, VERSION, SessionState, host_name, push
from .host_actions_protocol import MAX_EXCHANGE_BYTES, ActionKind, ActionState, results, trash_items
from . import host_queue
from .json_file import JsonFile
from .paths import HOSTS_DIR


class Hosts:
    def __init__(self, root: Path = HOSTS_DIR, clock=time.time, importer=None):
        self.root, self.clock = Path(root), clock
        self.importer = importer

    def file(self, host: str) -> JsonFile:
        return JsonFile(self.root / f"{host_name(host)}.json")

    def exchange(self, host: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("exchange must be an object")
        metadata = {k: v for k, v in payload.items() if k != "uploads"}
        if len(json.dumps(metadata).encode()) > MAX_METADATA_BYTES:
            raise ValueError("host metadata too large")
        clean = push(metadata)
        clean["trash"] = trash_items(payload.get("trash", []))
        outcomes = results(payload.get("results", []))
        uploads = payload.get("uploads", [])
        if not isinstance(uploads, list) or len(uploads) > 1:
            raise ValueError("invalid upload list")
        if type(payload.get("ready", True)) is not bool:
            raise ValueError("ready must be boolean")
        file = self.file(host)
        with file.locked():
            data = file.load()
            acknowledged = host_queue.apply_results(data, outcomes, uploads, self.clock(), self.receive)
            data.update(clean, last_seen=self.clock())
            actions = host_queue.dispatch(data, self.clock()) if payload.get("ready", True) else []
            file.write(data)
        reply = {"version": VERSION, "actions": actions}
        if outcomes:
            reply["acknowledged"] = acknowledged
        return reply

    def receive(self, item: dict, upload: dict) -> str:
        return self.get_importer().receive(item, upload)

    def get_importer(self):
        if self.importer is None:
            from .host_import import HostImporter
            self.importer = HostImporter()
        return self.importer

    def queue(self, host: str, sid: str, kind: str, body: dict) -> dict:
        file = self.file(host)
        with file.locked():
            data = file.load()
            item = host_queue.enqueue(data, sid, kind, body, self.clock())
            if kind == ActionKind.FORK_PI:
                self.get_importer().check(item["cwd"], item["body"]["title"])
            file.write(data)
        return {"ok": True, "action_id": item["id"], "state": item["state"]}

    def input_limit(self, host: str) -> int:
        data = self.file(host).load()
        host_queue.expire(data, self.clock())
        awaiting = any(i["action"] == ActionKind.FORK_PI and i["state"] == ActionState.SENT
                       for i in data.get("actions", []))
        return MAX_EXCHANGE_BYTES if awaiting else MAX_METADATA_BYTES

    def snapshot(self) -> dict:
        hosts, rows, trash, actions = [], [], [], []
        for path in sorted(self.root.glob("*.json")):
            try:
                host = host_name(path.stem)
            except ValueError:
                continue  # JsonFile's hidden temp file mid-write (.m4mac-*.json), or a stray file
            file = JsonFile(path)
            with file.locked():
                data = file.load()
                if host_queue.expire(data, self.clock()):
                    file.write(data)
            last_seen = data.get("last_seen", 0)
            online = self.clock() - last_seen < OFFLINE_AFTER
            hosts.append({"host": host, "online": online, "last_seen": last_seen})
            rows.extend(page_row(r, host, online, last_seen) for r in data.get("rows", []))
            trash.extend(page_row(r, host, online, last_seen) for r in data.get("trash", []))
            actions.extend({**a, "host": host} for a in data.get("actions", []))
        return {"hosts": hosts, "sessions": rows, "trash": trash, "actions": actions}


def page_row(row: dict, host: str, online: bool, last_seen: float) -> dict:
    sid, cwd = row["id"], row["cwd"]
    command = f"{'codex resume' if row['tool'] == 'codex' else 'claude --resume'} {shlex.quote(sid)}"
    return {**row, "id": f"{host}:{sid}", "session_id": sid, "host": host,
            "host_online": online, "last_seen": last_seen, "short_id": sid[:8],
            "project": cwd, "resume_cmd": f"cd -- {shlex.quote(cwd)} && {command}",
            "enabled": False, "new": False, "remote_url": "", "rc_name": "", "status": None,
            "open_elsewhere": [] if row["state"] == SessionState.CLOSED else [{"status": row["state"]}],
            "codex_state": "active" if row["state"] == SessionState.BUSY else "notLoaded"}
