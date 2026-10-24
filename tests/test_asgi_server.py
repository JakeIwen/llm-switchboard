"""Real Uvicorn listeners, late binds, peer refusal and lifespan ownership."""

import asyncio
from contextlib import asynccontextmanager
import socket
import threading
import unittest
import urllib.request
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_switchboard.http.server import Server, bind_socket, run_service
from llm_switchboard.http.lifespan import service_lifespan


class UvicornListenTest(unittest.IsolatedAsyncioTestCase):
    async def test_late_listener_retries_failed_bind_and_shuts_down_with_fixed_listener(self):
        starts, stops = [], []

        @asynccontextmanager
        async def lifespan(app):
            starts.append(True)
            yield
            stops.append(True)

        app = FastAPI(lifespan=lifespan)

        @app.get("/")
        def health():
            return {"ok": True}

        fixed = bind_socket("127.0.0.1", 0)
        answers = iter([[], ["bad-address"], ["127.0.0.1"]])
        server = Server(app, port=0, tailscale=True, resolve=lambda: next(answers), retry_s=0.01)
        task = asyncio.create_task(server.serve(sockets=[fixed]))
        try:
            async with asyncio.timeout(5):
                while len(getattr(server, "servers", [])) < 2:
                    await asyncio.sleep(0.01)
            ports = [listener.sockets[0].getsockname()[1] for listener in server.servers]
            self.assertEqual(len(set(ports)), 2)
            for port in ports:
                response = await asyncio.to_thread(urllib.request.urlopen, f"http://127.0.0.1:{port}/", timeout=2)
                with response:
                    self.assertEqual(response.read(), b'{"ok":true}')
            self.assertEqual(starts, [True])
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            fixed.close()
        self.assertEqual(stops, [True])
        for port in ports:
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.1)

    async def test_peer_refusal_closes_connection_without_an_http_response(self):
        sock = bind_socket("127.0.0.1", 0)
        server = Server(FastAPI(), port=sock.getsockname()[1])
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            with patch("llm_switchboard.listen.allowed_peer", return_value=False):
                reader, writer = await asyncio.open_connection("127.0.0.1", sock.getsockname()[1])
                writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nX-Forwarded-For: 127.0.0.1\r\n\r\n")
                try:
                    self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
                except ConnectionResetError:
                    pass  # Both FIN and RST are a refusal without HTTP bytes.
                finally:
                    writer.close()
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            sock.close()

    def test_busy_fixed_port_fails_before_lifespan_starts(self):
        with bind_socket("127.0.0.1", 0) as occupied:
            app = FastAPI()
            with self.assertLogs("llm_switchboard.http.server", level="ERROR"):
                self.assertEqual(run_service(app, ["127.0.0.1"], occupied.getsockname()[1]), 1)


class LifespanTest(unittest.TestCase):
    def test_supervisor_and_indexer_share_stop_and_do_not_stop_tmux(self):
        stopped = []
        started = threading.Event()

        class Supervisor:
            def run_forever(self, stop):
                started.set()
                stop.wait()
                stopped.append("supervisor")

            def wake(self):
                pass

        class Scheduler:
            def start(self, stop):
                def run():
                    stop.wait()
                    stopped.append("indexer")
                worker = threading.Thread(target=run)
                worker.start()
                return worker

        app = FastAPI(lifespan=service_lifespan)
        app.state.domain = type("Domain", (), {"supervisor": Supervisor()})()
        with patch("llm_switchboard.http.lifespan.SearchIndexScheduler", Scheduler):
            with TestClient(app):
                self.assertTrue(started.wait(2))
                self.assertEqual(stopped, [])
        self.assertEqual(set(stopped), {"supervisor", "indexer"})
