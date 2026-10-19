"""The SSH forced command: bounded JSON stdin, a metadata-only reply on stdout."""

import json

from .host_protocol import MAX_METADATA_BYTES
from .hosts import Hosts


def exchange(stdin, stdout, host: str, hosts: Hosts | None = None) -> None:
    raw = stdin.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("host exchange input too large")
    reply = (hosts or Hosts()).exchange(host, json.loads(raw))
    stdout.write(json.dumps(reply) + "\n")
