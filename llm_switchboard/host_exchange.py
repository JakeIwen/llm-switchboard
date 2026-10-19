"""The SSH forced command: bounded JSON stdin, a metadata-only reply on stdout."""

import json

from .hosts import Hosts


def exchange(stdin, stdout, host: str, hosts: Hosts | None = None) -> None:
    hosts = hosts or Hosts()
    limit = hosts.input_limit(host)
    raw = stdin.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("host exchange input too large")
    reply = hosts.exchange(host, json.loads(raw))
    stdout.write(json.dumps(reply) + "\n")
