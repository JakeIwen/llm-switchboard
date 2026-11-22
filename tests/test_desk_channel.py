import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from llm_switchboard.desk import protocol
from llm_switchboard.desk.config import DeskConfig
from llm_switchboard.desk.protocol import MessageType

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv" / "bin" / "python"
TIMEOUT = 10


def wait_for(condition, what="condition"):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class HubConnection:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def send(self, kind, **fields):
        self.sock.sendall(protocol.encode(kind, **fields))

    def receive(self, timeout=TIMEOUT):
        self.sock.settimeout(timeout)
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return protocol.decode(line)

    def close(self):
        self.sock.close()


class FakeHub:
    """Listens on the channel socket; the first `refuse` hellos are refused."""

    def __init__(self, path, refuse=0):
        self.refuse = refuse
        self.hellos = []
        self.connections = queue.Queue()
        self.opened = []
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            conn = HubConnection(sock)
            self.opened.append(conn)
            try:
                kind, hello = conn.receive()
            except (EOFError, OSError):
                continue
            self.hellos.append(hello["session_id"])
            if self.refuse > 0:
                self.refuse -= 1
                conn.send(MessageType.REFUSED, reason="not enrolled")
                conn.close()
                continue
            conn.send(MessageType.WELCOME, epoch="e", generation=1)
            self.connections.put(conn)

    def next_connection(self):
        return self.connections.get(timeout=TIMEOUT)

    def close(self):
        self.server.close()
        for conn in self.opened:
            conn.close()


class ChannelProcess:
    def __init__(self, home, session_id="sess-1"):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "CLAUDE_CODE_SESSION_ID": session_id}
        self.proc = subprocess.Popen(
            [str(PYTHON), "-I", "-B", str(REPO / "bin" / "desk-channel")], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(json.loads(line))

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def send_raw(self, data: bytes):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def read(self):
        return self.lines.get(timeout=TIMEOUT)

    def request(self, request_id, method, params=None):
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        message = self.read()
        self.assert_id(message, request_id)
        return message

    @staticmethod
    def assert_id(message, request_id):
        if message.get("id") != request_id:
            raise AssertionError(f"expected response {request_id}, got {message}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.proc.stderr.close()


@unittest.skipUnless(PYTHON.exists(), "needs the repo .venv python")
class ChannelServerTest(unittest.TestCase):
    def setUp(self):
        # A short path: Unix socket paths are limited to about 100 bytes.
        self.home = Path(tempfile.mkdtemp(prefix="dk", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.config = DeskConfig.from_env(home=self.home, environ={})
        self.channels = []
        self.hubs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for channel in self.channels:
            channel.stop()
        for hub in self.hubs:
            hub.close()

    def start_hub(self, refuse=0):
        hub = FakeHub(self.config.channel_socket, refuse)
        self.hubs.append(hub)
        return hub

    def start_channel(self):
        channel = ChannelProcess(self.home)
        self.channels.append(channel)
        return channel

    def connected(self, refuse=0):
        hub = self.start_hub(refuse)
        channel = self.start_channel()
        return hub, channel, hub.next_connection()

    def call_reply(self, channel, rpc_id, arguments):
        return channel.request(rpc_id, "tools/call", {"name": "reply", "arguments": arguments})

    def test_initialize_lists_reply_tool_and_never_declares_permission_relay(self):
        channel = self.start_channel()
        response = channel.request(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertEqual(result["capabilities"], {"experimental": {"claude/channel": {}}, "tools": {}})
        self.assertEqual(result["serverInfo"]["name"], "desk")
        self.assertEqual(result["instructions"], protocol.CHANNEL_INSTRUCTIONS)
        self.assertNotIn("claude/channel/permission", json.dumps(result))
        channel.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = channel.request(2, "tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["reply"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["request_id", "text", "spoken"])

    def test_initialize_without_version_falls_back(self):
        channel = self.start_channel()
        self.assertEqual(channel.request(1, "initialize")["result"]["protocolVersion"], "2025-06-18")

    def test_ping_unknown_method_and_malformed_lines(self):
        channel = self.start_channel()
        channel.send_raw(b"not json\n[1,2]\n{\"jsonrpc\":\"2.0\"}\n\n")
        self.assertEqual(channel.request(1, "ping")["result"], {})
        error = channel.request(2, "resources/list")["error"]
        self.assertEqual(error["code"], -32601)
        channel.send({"jsonrpc": "2.0", "method": "notifications/whatever"})
        self.assertEqual(channel.request(3, "ping")["result"], {})

    def test_event_becomes_the_channel_notification_and_is_acknowledged(self):
        hub, channel, conn = self.connected()
        self.assertEqual(hub.hellos, ["sess-1"])
        conn.send(MessageType.EVENT, request_id="r-1", content="open the hatch\n\nrule")
        self.assertEqual(channel.read(), {
            "jsonrpc": "2.0", "method": "notifications/claude/channel",
            "params": {"content": "open the hatch\n\nrule", "meta": {"request_id": "r-1"}}})
        kind, message = conn.receive()
        self.assertEqual((kind, message["request_id"]), (MessageType.DELIVERED, "r-1"))

    def test_reply_round_trip_and_hub_refusal(self):
        _, channel, conn = self.connected()
        channel.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                      "params": {"name": "reply", "arguments": {"request_id": "r-1", "text": "done", "spoken": "Done."}}})
        kind, message = conn.receive()
        self.assertEqual(kind, MessageType.REPLY)
        self.assertEqual((message["request_id"], message["text"], message["spoken"]), ("r-1", "done", "Done."))
        conn.send(MessageType.REPLY_RESULT, request_id="r-1", ok=True, reason="")
        result = channel.read()
        self.assertEqual(result["id"], 5)
        self.assertEqual(result["result"], {"content": [{"type": "text", "text": "Delivered to Jacob's desk."}]})

        channel.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                      "params": {"name": "reply", "arguments": {"request_id": "r-2", "text": "x", "spoken": "x"}}})
        conn.receive()
        conn.send(MessageType.REPLY_RESULT, request_id="r-2", ok=False, reason="request already answered")
        result = channel.read()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("request already answered", result["content"][0]["text"])

    def test_pings_are_answered_without_reaching_claude(self):
        _, channel, conn = self.connected()
        conn.send(MessageType.PING)
        self.assertEqual(conn.receive()[0], MessageType.PONG)
        self.assertEqual(channel.request(1, "ping")["result"], {})

    def test_reply_without_hub_is_a_tool_error(self):
        channel = self.start_channel()
        result = self.call_reply(channel, 1, {"request_id": "r", "text": "a", "spoken": "b"})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("not connected to the desk hub", result["content"][0]["text"])

    def test_invalid_and_oversize_replies_are_refused_locally(self):
        _, channel, conn = self.connected()
        cases = [
            {"request_id": "r", "text": "x" * (protocol.MAX_REPLY_TEXT_BYTES + 1), "spoken": "s"},
            {"request_id": "r", "text": "t", "spoken": "s" * (protocol.MAX_SPOKEN_BYTES + 1)},
            {"request_id": "r", "text": "t"},
            {"request_id": 7, "text": "t", "spoken": "s"},
        ]
        for number, arguments in enumerate(cases, start=1):
            result = self.call_reply(channel, number, arguments)["result"]
            self.assertTrue(result["isError"], arguments.keys())
        # Nothing reached the hub: its next message is the answer to this ping.
        channel.request(99, "ping")
        conn.send(MessageType.PING)
        self.assertEqual(conn.receive()[0], MessageType.PONG)

    def test_refusal_and_disconnect_lead_to_reconnects(self):
        hub = self.start_hub(refuse=1)
        self.start_channel()
        conn = hub.next_connection()
        self.assertEqual(len(hub.hellos), 2)
        conn.close()
        wait_for(lambda: len(hub.hellos) == 3, "reconnect after disconnect")

    def test_hub_appearing_later_is_picked_up(self):
        channel = self.start_channel()
        time.sleep(0.3)
        hub = self.start_hub()
        conn = hub.next_connection()
        conn.send(MessageType.EVENT, request_id="late", content="hello")
        self.assertEqual(channel.read()["params"]["meta"], {"request_id": "late"})

    def test_stdin_eof_exits_zero(self):
        channel = self.start_channel()
        channel.proc.stdin.close()
        self.assertEqual(channel.proc.wait(TIMEOUT), 0)

    def test_sigterm_exits_zero(self):
        channel = self.start_channel()
        channel.request(1, "ping")
        channel.proc.terminate()
        self.assertEqual(channel.proc.wait(TIMEOUT), 0)


class LightImportTest(unittest.TestCase):
    def test_channel_imports_only_the_standard_library(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import llm_switchboard.desk.channel; "
                "heavy = [m for m in ('fastapi', 'starlette', 'uvicorn', 'pydantic') if m in sys.modules]; "
                "print(','.join(heavy))")
        out = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(REPO)],
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
