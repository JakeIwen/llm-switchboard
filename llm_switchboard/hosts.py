"""Host snapshots, received through SSH and read by the web service."""

from pathlib import Path
import shlex
import time

from .host_protocol import OFFLINE_AFTER, VERSION, SessionState, host_name, push
from .json_file import JsonFile
from .paths import HOSTS_DIR


class Hosts:
    def __init__(self, root: Path = HOSTS_DIR, clock=time.time):
        self.root, self.clock = Path(root), clock

    def file(self, host: str) -> JsonFile:
        return JsonFile(self.root / f"{host_name(host)}.json")

    def exchange(self, host: str, payload: dict) -> dict:
        clean = push(payload)
        file = self.file(host)
        with file.locked():
            file.write({**clean, "last_seen": self.clock()})
        return {"version": VERSION, "actions": []}

    def snapshot(self) -> dict:
        hosts, rows = [], []
        for path in sorted(self.root.glob("*.json")):
            host = host_name(path.stem)
            data = JsonFile(path).load()
            last_seen = data.get("last_seen", 0)
            online = self.clock() - last_seen < OFFLINE_AFTER
            hosts.append({"host": host, "online": online, "last_seen": last_seen})
            rows.extend(page_row(r, host, online, last_seen) for r in data.get("rows", []))
        return {"hosts": hosts, "sessions": rows}


def page_row(row: dict, host: str, online: bool, last_seen: float) -> dict:
    sid, cwd = row["id"], row["cwd"]
    command = f"{'codex resume' if row['tool'] == 'codex' else 'claude --resume'} {shlex.quote(sid)}"
    return {**row, "id": f"{host}:{sid}", "session_id": sid, "host": host,
            "host_online": online, "last_seen": last_seen, "short_id": sid[:8],
            "project": cwd, "resume_cmd": f"cd -- {shlex.quote(cwd)} && {command}",
            "enabled": False, "new": False, "remote_url": "", "rc_name": "", "status": None,
            "open_elsewhere": [] if row["state"] == SessionState.CLOSED else [{"status": row["state"]}],
            "codex_state": "active" if row["state"] == SessionState.BUSY else "notLoaded"}
