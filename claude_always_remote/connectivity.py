"""Whether Anthropic's API is reachable, so sessions aren't started before the
uplink is (at boot, or while the van has no internet)."""

import socket
import time

API_HOST = "api.anthropic.com"


class Connectivity:
    def __init__(self, host: str = API_HOST, port: int = 443, ttl: float = 30.0, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.ttl = ttl
        self.timeout = timeout
        self._checked_at = 0.0
        self._ok = False

    def ok(self) -> bool:
        now = time.monotonic()
        if now - self._checked_at >= self.ttl:
            self._ok = self._probe()
            self._checked_at = now
        return self._ok

    def _probe(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                return True
        except OSError:
            return False
