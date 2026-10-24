"""Uvicorn sockets: fixed binds fail early, Tailscale may appear after boot."""

import asyncio
import contextlib
import logging
import signal
import socket
import threading

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .. import listen

log = logging.getLogger(__name__)


class PeerProtocol(H11Protocol):
    """Refuse disallowed peers before HTTP, as the former verify_request did."""

    def connection_made(self, transport):
        super().connection_made(transport)
        self.peer_allowed = bool(self.client and listen.allowed_peer(self.client[0]))
        if not self.peer_allowed:
            transport.abort()

    def data_received(self, data):
        if self.peer_allowed:
            super().data_received(data)


def bind_socket(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sock.family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        sock.listen(2048)
        sock.setblocking(False)
        log.info("picker on http://%s:%d", f"[{host}]" if ":" in host else host, sock.getsockname()[1])
        return sock
    except BaseException:
        sock.close()
        raise


class Server(uvicorn.Server):
    def __init__(self, app, *, port, tailscale=False, resolve=None, retry_s=30.0):
        super().__init__(uvicorn.Config(app, http=PeerProtocol, loop="asyncio", ws="none",
                                        proxy_headers=False, access_log=False, log_config=None,
                                        lifespan="on", timeout_graceful_shutdown=10))
        self.port, self.tailscale = port, tailscale
        self.resolve = resolve or listen.tailscale_addresses
        self.retry_s = retry_s
        self.bind_task = None

    @contextlib.contextmanager
    def capture_signals(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        handlers = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        # Unlike Uvicorn's default, a completed graceful shutdown exits normally;
        # re-raising SIGTERM would report a killed process to our service manager.

    async def startup(self, sockets=None):
        await super().startup(sockets)
        if self.tailscale and not self.should_exit:
            self.bind_task = asyncio.create_task(self._bind_tailscale(), name="tailscale-bind")

    async def shutdown(self, sockets=None):
        if self.bind_task is not None:
            self.bind_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.bind_task
        await super().shutdown(sockets)

    async def _bind_tailscale(self):
        warned = False
        while not self.should_exit:
            addresses = await asyncio.to_thread(self.resolve)
            bound = False
            for address in addresses:
                try:
                    await self._add_listener(address)
                    bound = True
                except OSError as exc:
                    log.warning("can't listen on Tailscale address %s: %s", address, exc)
            if bound:
                return
            if not warned:
                log.warning("not listening on Tailscale yet; retrying every %.0fs", self.retry_s)
                warned = True
            await asyncio.sleep(self.retry_s)

    async def _add_listener(self, address):
        sock = bind_socket(address, self.port)
        try:
            loop = asyncio.get_running_loop()
            # Protocol construction and server/lifespan state are Uvicorn internals
            # pinned by uv.lock; an upgrade is a listener/lifecycle contract change.
            server = await loop.create_server(
                lambda: PeerProtocol(config=self.config, server_state=self.server_state,
                                     app_state=self.lifespan.state, _loop=loop), sock=sock)
            self.servers.append(server)
        except BaseException:
            sock.close()
            raise


def run_service(app, hosts: list[str], port: int) -> int:
    sockets = []
    try:
        for host in hosts:
            if host != listen.TAILSCALE:
                sockets.append(bind_socket(host, port))
        Server(app, port=port, tailscale=listen.TAILSCALE in hosts).run(sockets=sockets)
        return 0
    except OSError as exc:
        log.error("cannot start picker: %s", exc)
        return 1
    finally:
        for sock in sockets:
            sock.close()


class BoundServer:
    """One-address foreground server for integration callers of make_server."""

    def __init__(self, app, host, port):
        self.socket = bind_socket(host, port)
        self.server_address = self.socket.getsockname()
        self.server_port = self.server_address[1]
        self.server = Server(app, port=self.server_port)
        self.finished = threading.Event()

    def serve_forever(self):
        try:
            self.server.run(sockets=[self.socket])
        finally:
            self.finished.set()

    def shutdown(self):
        self.server.should_exit = True
        self.finished.wait(15)

    def server_close(self):
        self.socket.close()
