"""Where the picker listens: localhost and this machine's Tailscale addresses, never the LAN.

Binding to specific addresses keeps the port off other interfaces, but Linux
accepts a packet for any local address on any interface, so a LAN host that
routes the Tailscale address through us could still connect. `allowed_peer`
closes that: Tailscale's firewall drops tailnet-range sources that don't
arrive on tailscale0, so a peer in those ranges really came over Tailscale.
"""

import ipaddress
import logging
import subprocess
import threading
from typing import Callable

log = logging.getLogger(__name__)

# A --host value that stands for this machine's Tailscale addresses.
TAILSCALE = "tailscale"

_TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def allowed_peer(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_loopback or any(addr in net for net in _TAILNET)


def tailscale_addresses() -> list[str]:
    """This machine's Tailscale IPv4 address, or [] while Tailscale is down or missing.

    IPv4 only: clients reach it whichever family MagicDNS hands them first, and
    some kernels (this Pi's) can't open IPv6 sockets at all.
    """
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return out.stdout.split() if out.returncode == 0 else []


class Listeners:
    """One server per address, all serving the same app.

    Fixed addresses bind at once, so a busy port fails the start. The Tailscale
    addresses may not exist yet at boot (tailscaled starts on its own), so they
    are looked up again every `retry_s` until they bind.
    """

    def __init__(self, hosts: list[str], port: int, make: Callable[[str, int], object],
                 resolve_tailscale: Callable[[], list[str]] = tailscale_addresses, retry_s: float = 30.0):
        self._port, self._make, self._resolve, self._retry_s = port, make, resolve_tailscale, retry_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.servers = []
        self._want_tailscale = TAILSCALE in hosts
        for host in hosts:
            if host != TAILSCALE:
                self._start(host)

    def serve(self) -> None:
        """Serve until `shutdown()`."""
        if self._want_tailscale:
            threading.Thread(target=self._bind_tailscale, name="tailscale-bind", daemon=True).start()
        self._stop.wait()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            servers = list(self.servers)
        for server in servers:
            server.shutdown()
            server.server_close()

    def _start(self, host: str) -> None:
        server = self._make(host, self._port)
        with self._lock:
            if self._stop.is_set():
                server.server_close()
                return
            self.servers.append(server)
        threading.Thread(target=server.serve_forever, name=f"http-{host}", daemon=True).start()
        log.info("picker on http://%s:%d", f"[{host}]" if ":" in host else host, self._port)

    def _bind_tailscale(self) -> None:
        warned = False
        while not self._stop.is_set():
            bound = 0
            for addr in self._resolve():
                try:
                    self._start(addr)
                    bound += 1
                except OSError as e:
                    log.warning("can't listen on Tailscale address %s: %s", addr, e)
            if bound:
                return
            if not warned:
                log.warning("not listening on Tailscale yet; retrying every %.0fs", self._retry_s)
                warned = True
            self._stop.wait(self._retry_s)
