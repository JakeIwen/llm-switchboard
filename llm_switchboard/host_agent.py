"""Outward-only Mac publisher. SSH is the only transport; this opens no listener."""

import json
import logging
import subprocess
import time
from pathlib import Path

from .host_protocol import MAX_METADATA_BYTES, PUSH_INTERVAL, VERSION, host_name
from .mac_catalog import MacCatalog

log = logging.getLogger(__name__)


class HostAgent:
    def __init__(self, pi: str, host: str, identity: Path, catalog=None, run=subprocess.run):
        self.host = host_name(host)
        if not pi or pi.startswith("-") or any(c.isspace() for c in pi):
            raise ValueError("pi must be an SSH host alias")
        self.pi, self.identity = pi, Path(identity).expanduser().resolve()
        self.catalog, self.run = catalog or MacCatalog(), run
        self.control = self.identity.parent / "switchboard-%C"

    def exchange(self, payload: dict) -> dict:
        raw = json.dumps(payload)
        if len(raw.encode()) > MAX_METADATA_BYTES:
            raise ValueError("host metadata exceeds the exchange size limit")
        argv = ["ssh", "-T", "-i", str(self.identity), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                "-o", "ControlMaster=auto", "-o", f"ControlPath={self.control}", "-o", "ControlPersist=60",
                self.pi, "host-exchange", self.host]
        result = self.run(argv, input=raw, text=True, capture_output=True, timeout=25, check=True)
        if len(result.stdout.encode()) > MAX_METADATA_BYTES:
            raise ValueError("oversized host reply")
        reply = json.loads(result.stdout)
        if not isinstance(reply, dict) or reply.get("version") != VERSION or not isinstance(reply.get("actions"), list):
            raise ValueError("invalid host reply")
        return reply

    def cycle(self) -> dict:
        # Phase-one publishers never execute anything the Pi sends back.
        return self.exchange({"version": VERSION, "rows": self.catalog.rows()})

    def run_forever(self) -> None:
        while True:
            start = time.monotonic()
            try:
                self.cycle()
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log.warning("host exchange failed: %s", e)
            time.sleep(max(0, PUSH_INTERVAL - (time.monotonic() - start)))
