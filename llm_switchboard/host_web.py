"""Separate remote routes: a host-qualified id can never reach local actions."""

import re

from .host_protocol import HOST_PATTERN

_ROUTE = re.compile(rf"^/api/hosts/({HOST_PATTERN})/sessions/([^/]+)/([a-z-]+)$")


def action_route(path: str):
    match = _ROUTE.fullmatch(path)
    return match.groups() if match else None
