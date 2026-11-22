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
from llm_switchboard.desk.channel import HubLink, MAX_MCP_LINE_BYTES, REPLY_WAIT_SECONDS, StdoutWriter
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


def channel_wait_limit():
    """Longer than the channel's own reply wait, which is what these tests wait out."""
    return REPLY_WAIT_SECONDS + TIMEOUT


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
        self.write_session("sess-1")

    def write_session(self, session_id, pid=None):
        """The registry entry of the channel's parent, which is this test process."""
        pid = os.getpid() if pid is None else pid
        self.config.claude_sessions_dir.mkdir(parents=True, exist_ok=True)
        entry = {"pid": pid, "procStart": "1", "sessionId": session_id}
        (self.config.claude_sessions_dir / f"{pid}.json").write_text(json.dumps(entry))

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

    def assert_event_undeliverable(self, conn, channel, request_id):
        conn.send(MessageType.EVENT, request_id=request_id, content="secret prompt")
        kind, message = conn.receive()
        self.assertEqual((kind, message["request_id"]), (MessageType.UNDELIVERABLE, request_id))
        self.assertTrue(message["reason"])
        # The link is dropped, and nothing reached stdout: the next line is the ping answer.
        with self.assertRaises(EOFError):
            conn.receive()
        self.assertEqual(channel.request(90, "ping")["result"], {})

    def test_event_is_not_written_when_the_parent_session_changed(self):
        _, channel, conn = self.connected()
        self.write_session("sess-after-clear")  # /clear between the hub's check and the write
        self.assert_event_undeliverable(conn, channel, "r-1")

    def test_event_is_not_written_when_the_session_file_is_missing_or_unreadable(self):
        for damage in (lambda path: path.unlink(), lambda path: path.write_text("{not json")):
            with self.subTest(damage=damage):
                self.write_session("sess-1")
                hub, channel, conn = self.connected()
                damage(self.config.claude_sessions_dir / f"{os.getpid()}.json")
                self.assert_event_undeliverable(conn, channel, "r-1")
                self.cleanup()
                self.channels, self.hubs = [], []
                self.config.channel_socket.unlink(missing_ok=True)

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

    def test_unencodable_and_over_framed_replies_get_tool_errors(self):
        _, channel, conn = self.connected()
        cases = [
            # A lone surrogate cannot be strict UTF-8.
            {"request_id": "r", "text": "bad \ud800 text", "spoken": "s"},
            {"request_id": "r", "text": "t", "spoken": "\udfff"},
            {"request_id": "r\ud800", "text": "t", "spoken": "s"},
            # 15 KiB of "é" is 30 KiB raw but 6 bytes per character once JSON-escaped.
            {"request_id": "r", "text": "\u00e9" * (15 * 1024), "spoken": "s"},
            # Quotes escape to two bytes each: 32 KiB raw, over 64 KiB framed.
            {"request_id": "r", "text": '"' * protocol.MAX_REPLY_TEXT_BYTES, "spoken": "s"},
        ]
        for number, arguments in enumerate(cases, start=1):
            result = self.call_reply(channel, number, arguments)["result"]
            self.assertTrue(result["isError"], number)
        channel.request(99, "ping")
        conn.send(MessageType.PING)
        self.assertEqual(conn.receive()[0], MessageType.PONG)

    def reply_call(self, channel, rpc_id, request_id):
        channel.send({"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                      "params": {"name": "reply", "arguments": {"request_id": request_id, "text": "t", "spoken": "s"}}})

    def test_second_reply_while_the_first_waits_is_refused_locally(self):
        _, channel, conn = self.connected()
        self.reply_call(channel, 1, "r-1")
        self.assertEqual(conn.receive()[0], MessageType.REPLY)
        self.reply_call(channel, 2, "r-1")
        refused = channel.read()
        channel.assert_id(refused, 2)
        self.assertTrue(refused["result"]["isError"])
        self.assertIn("already in flight", refused["result"]["content"][0]["text"])
        # The first call still gets its own result, and the hub saw only one reply.
        conn.send(MessageType.REPLY_RESULT, request_id="r-1", ok=True, reason="")
        first = channel.read()
        channel.assert_id(first, 1)
        self.assertNotIn("isError", first["result"])
        conn.send(MessageType.PING)
        self.assertEqual(conn.receive()[0], MessageType.PONG)

    def test_unconfirmed_reply_resets_the_link_so_a_late_ack_cannot_confirm_a_retry(self):
        hub, channel, old = self.connected()
        self.reply_call(channel, 1, "r-1")
        self.assertEqual(old.receive()[0], MessageType.REPLY)
        timed_out = channel.lines.get(timeout=channel_wait_limit())  # the hub never answers within the wait
        channel.assert_id(timed_out, 1)
        text = timed_out["result"]["content"][0]["text"]
        self.assertTrue(timed_out["result"]["isError"])
        self.assertIn("link was reset", text)
        with self.assertRaises(EOFError):
            old.receive()
        new = hub.next_connection()
        # Submit the retry only once the channel has consumed WELCOME and serves the new link.
        new.send(MessageType.PING)
        self.assertEqual(new.receive()[0], MessageType.PONG)
        self.reply_call(channel, 2, "r-1")
        kind, message = new.receive()
        self.assertEqual((kind, message["request_id"]), (MessageType.REPLY, "r-1"))
        try:  # the first attempt's delayed success, on the connection that was dropped
            old.send(MessageType.REPLY_RESULT, request_id="r-1", ok=True, reason="")
        except OSError:
            pass
        channel.request(50, "ping")  # nothing may have answered call 2 from that ack
        self.assertTrue(channel.lines.empty())
        new.send(MessageType.REPLY_RESULT, request_id="r-1", ok=False, reason="wrong connection")
        retry = channel.read()
        channel.assert_id(retry, 2)
        self.assertTrue(retry["result"]["isError"])
        self.assertIn("wrong connection", retry["result"]["content"][0]["text"])

    def test_reply_after_the_first_finished_goes_to_the_hub_again(self):
        _, channel, conn = self.connected()
        self.reply_call(channel, 1, "r-1")
        conn.receive()
        conn.send(MessageType.REPLY_RESULT, request_id="r-1", ok=True, reason="")
        channel.assert_id(channel.read(), 1)
        self.reply_call(channel, 2, "r-1")
        kind, message = conn.receive()
        self.assertEqual((kind, message["request_id"]), (MessageType.REPLY, "r-1"))
        conn.send(MessageType.REPLY_RESULT, request_id="r-1", ok=False, reason="request already answered")
        second = channel.read()
        channel.assert_id(second, 2)
        self.assertIn("already answered", second["result"]["content"][0]["text"])

    def test_oversize_stdin_line_is_skipped_and_later_lines_still_work(self):
        channel = self.start_channel()
        # A well-formed request that is simply too big is discarded unanswered, not buffered and served.
        channel.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"p":"' + b"x" * (3 * MAX_MCP_LINE_BYTES) + b'"}}\n')
        self.assertEqual(channel.request(3, "ping")["result"], {})
        # A line of exactly the cap is still accepted.
        pad = MAX_MCP_LINE_BYTES - len(b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{"p":""}}\n')
        channel.send_raw(b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{"p":"' + b"a" * pad + b'"}}\n')
        channel.assert_id(channel.read(), 2)

    def test_concurrent_calls_are_bounded_and_ping_stays_responsive(self):
        _, channel, conn = self.connected()
        for number in range(8):
            self.reply_call(channel, 100 + number, f"r-{number}")
        for _ in range(8):
            self.assertEqual(conn.receive()[0], MessageType.REPLY)
        self.reply_call(channel, 200, "r-extra")
        busy = channel.read()
        channel.assert_id(busy, 200)
        self.assertEqual(busy["error"]["code"], -32000)
        self.assertEqual(channel.request(201, "ping")["result"], {})
        # A freed slot accepts the next call.
        conn.send(MessageType.REPLY_RESULT, request_id="r-0", ok=True, reason="")
        channel.assert_id(channel.read(), 100)
        self.reply_call(channel, 202, "r-again")
        self.assertEqual(conn.receive()[0], MessageType.REPLY)

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


class HubLinkWaiterTest(unittest.TestCase):
    def test_a_finished_call_cannot_remove_a_replacement_waiter(self):
        home = Path(tempfile.mkdtemp(prefix="dk", dir="/tmp"))
        self.addCleanup(shutil.rmtree, home, True)
        link = HubLink(DeskConfig.from_env(home=home, environ={}), "sess-1", StdoutWriter(None))
        old, replacement = {"done": threading.Event()}, {"done": threading.Event()}
        link._pending["r-1"] = replacement
        link._forget("r-1", old)
        self.assertIs(link._pending.get("r-1"), replacement)
        link._forget("r-1", replacement)
        self.assertNotIn("r-1", link._pending)


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
