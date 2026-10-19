"""Outward-only Mac publisher. SSH is the only transport; this opens no listener."""

import json
import logging
import subprocess
import time
from pathlib import Path

from .host_protocol import MAX_METADATA_BYTES, PUSH_INTERVAL, VERSION, host_name
from .host_transport import bounded_ssh
from .mac_catalog import MacCatalog
from .mac_actions import MacActions
from .mac_action_runner import MacActionRunner
from .host_actions_protocol import MAX_EXCHANGE_BYTES

log = logging.getLogger(__name__)


class HostAgent:
    def __init__(self, pi: str, host: str, identity: Path, catalog=None, run=bounded_ssh, executor=None, runner=None):
        self.host = host_name(host)
        if not pi or pi.startswith("-") or any(c.isspace() for c in pi):
            raise ValueError("pi must be an SSH host alias")
        self.pi, self.identity = pi, Path(identity).expanduser().resolve()
        self.catalog, self.run = catalog or MacCatalog(), run
        self.executor = executor or MacActions(self.catalog)
        self.runner = runner or MacActionRunner(self.executor)
        self.control = self.identity.parent / "switchboard-%C"

    def exchange(self, payload: dict) -> dict:
        raw = json.dumps(payload)
        if len(raw.encode()) > (MAX_EXCHANGE_BYTES if payload.get("uploads") else MAX_METADATA_BYTES):
            raise ValueError("host metadata exceeds the exchange size limit")
        argv = ["ssh", "-T", "-i", str(self.identity), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                "-o", "ControlMaster=auto", "-o", f"ControlPath={self.control}", "-o", "ControlPersist=60",
                self.pi, "host-exchange", self.host]
        result = self.run(argv, input=raw, text=True, capture_output=True, timeout=25, check=True)
        if len(result.stdout.encode()) > MAX_METADATA_BYTES:
            raise ValueError("oversized host reply")
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict) or type(reply.get("version")) is not int or reply["version"] != VERSION or not isinstance(reply.get("actions"), list):
            raise ValueError("invalid host reply")
        return reply

    def payload(self, *, results=None, uploads=None, ready=True) -> dict:
        rows = self.catalog.rows()
        return {"version": VERSION, "rows": rows, "trash": self.executor.trash_rows(),
                "results": self.runner.pending() if results is None else results,
                "uploads": uploads or [], "ready": ready}

    def cycle(self) -> dict:
        payload = self.payload()
        start, wall = time.monotonic(), time.time()
        reply = self.exchange(payload)
        self.runner.acknowledge(reply.get("acknowledged", []))
        if reply["actions"]:
            elapsed = max(time.monotonic() - start, time.time() - wall, 0)
            results, uploads = self.runner.run(reply["actions"], elapsed)
            # No new delivery during the follow-up: it only acknowledges this batch.
            reply = self.exchange(self.payload(results=results, uploads=uploads, ready=False))
            self.runner.acknowledge(reply.get("acknowledged", []))
        return reply

    def run_forever(self) -> None:
        while True:
            start = time.monotonic()
            try:
                self.cycle()
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log.warning("host exchange failed: %s", e)
            time.sleep(max(0, PUSH_INTERVAL - (time.monotonic() - start)))
