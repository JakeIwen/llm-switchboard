"""The desk link's forced command (`bin/desk-link`, desk/link_pump.py): whole
lines both ways, never a partial one, each line's length checked before it
is forwarded, the no-hub frame, and the one exit line."""

import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from desk_fixtures import desk_config, make_home

from llm_switchboard.desk.link_protocol import (
    LINK_VERSIONS, MAX_LINK_LINE_BYTES, LinkType, RefusedCode, Side, decode_frame,
)
from llm_switchboard.desk.link_pump import PumpExit, pump

REPO = Path(__file__).resolve().parent.parent
# Generous: the Pi runs the suite at load 10 to 20.
WAIT_SECONDS = 30


def line_of(size: int, fill: bytes = b"a") -> bytes:
    """A line of exactly `size` bytes, its newline included."""
    return fill * (size - 1) + b"\n"


def recv_all(conn: socket.socket) -> bytes:
    chunks = []
    while chunk := conn.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


class PumpRun:
    """The pump in a thread between two pipes (the SSH channel) and a
    listening socket standing in for the hub's `link.sock`."""

    def __init__(self, case: unittest.TestCase, listen: bool = True):
        self.dir = Path(tempfile.mkdtemp(prefix="pump", dir="/tmp"))
        case.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "link.sock"
        self.listener = None
        if listen:
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.path))
            self.listener.listen(1)
            case.addCleanup(self.listener.close)
        self.stdin_r, self.stdin_w = os.pipe()
        self.stdout_r, self.stdout_w = os.pipe()
        for fd in (self.stdin_r, self.stdin_w, self.stdout_r, self.stdout_w):
            case.addCleanup(_close_quietly, fd)
        self.err = io.StringIO()
        self.status = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.hub = None
        if listen:
            self.listener.settimeout(WAIT_SECONDS)
            self.hub, _ = self.listener.accept()
            self.hub.settimeout(WAIT_SECONDS)
            case.addCleanup(self.hub.close)
        # The relay's stdout reads end at EOF once the pump closes its end.
        self._stdout = bytearray()
        self._stdout_grew = threading.Condition()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _run(self):
        self.status = pump(self.path, self.stdin_r, self.stdout_w, self.err)

    def _read_stdout(self):
        while chunk := os.read(self.stdout_r, 65536):
            with self._stdout_grew:
                self._stdout += chunk
                self._stdout_grew.notify_all()

    def wait_relay_bytes(self, count: int) -> None:
        with self._stdout_grew:
            self._stdout_grew.wait_for(lambda: len(self._stdout) >= count, WAIT_SECONDS)

    def relay_sends(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.stdin_w, view):]

    def relay_closes(self) -> None:
        _close_quietly(self.stdin_w)

    def finish(self) -> tuple[int, str]:
        self.thread.join(WAIT_SECONDS)
        assert not self.thread.is_alive(), "the pump never exited"
        # The process would exit here, closing stdout; in a test, close it.
        _close_quietly(self.stdout_w)
        return self.status, self.err.getvalue()

    def relay_received(self) -> bytes:
        self._reader.join(WAIT_SECONDS)
        return bytes(self._stdout)


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class PumpEofTest(unittest.TestCase):
    def test_relay_eof_forwards_whole_lines_and_drops_the_partial_one(self):
        run = PumpRun(self)
        run.relay_sends(b'{"a":1}\n{"b":2}\n{"partial":')
        run.relay_closes()
        self.assertEqual(run.finish(), (0, "desk-link: relay_closed\n"))
        self.assertEqual(recv_all(run.hub), b'{"a":1}\n{"b":2}\n')

    def test_hub_eof_forwards_whole_lines_and_drops_the_partial_one(self):
        run = PumpRun(self)
        run.hub.sendall(b'{"c":3}\n{"partial":')
        run.hub.shutdown(socket.SHUT_WR)
        self.assertEqual(run.finish(), (0, "desk-link: hub_closed\n"))
        self.assertEqual(run.relay_received(), b'{"c":3}\n')

    def test_hub_eof_ends_the_pump_while_the_relay_stays_open(self):
        run = PumpRun(self)
        run.hub.close()
        self.assertEqual(run.finish(), (0, "desk-link: hub_closed\n"))
        self.assertEqual(run.relay_received(), b"")

    def test_relay_eof_closes_the_hubs_side(self):
        run = PumpRun(self)
        run.relay_closes()
        self.assertEqual(run.finish()[0], 0)
        self.assertEqual(recv_all(run.hub), b"")

    def test_a_line_split_across_reads_is_forwarded_whole(self):
        run = PumpRun(self)
        run.relay_sends(b'{"long":"')
        run.relay_sends(b'value"}\n')
        run.relay_closes()
        run.finish()
        self.assertEqual(recv_all(run.hub), b'{"long":"value"}\n')


class PumpLimitTest(unittest.TestCase):
    def test_a_line_exactly_at_the_cap_passes_both_ways(self):
        run = PumpRun(self)
        up = line_of(MAX_LINK_LINE_BYTES, b"u")
        down = line_of(MAX_LINK_LINE_BYTES, b"d")
        sender = threading.Thread(target=run.hub.sendall, args=(down,), daemon=True)
        sender.start()
        run.relay_sends(up)
        received = b""
        while len(received) < len(up):
            received += run.hub.recv(65536)
        self.assertEqual(received, up)
        run.wait_relay_bytes(len(down))
        run.relay_closes()
        self.assertEqual(run.finish(), (0, "desk-link: relay_closed\n"))
        self.assertEqual(run.relay_received(), down)

    def test_one_byte_over_the_cap_from_the_relay_exits_4_forwarding_nothing_of_it(self):
        run = PumpRun(self)
        # The line before it in the same write is still forwarded.
        run.relay_sends(b'{"ok":1}\n' + line_of(MAX_LINK_LINE_BYTES + 1))
        self.assertEqual(run.finish(), (4, "desk-link: oversize\n"))
        self.assertEqual(recv_all(run.hub), b'{"ok":1}\n')

    def test_an_over_cap_partial_line_exits_before_its_newline(self):
        run = PumpRun(self)
        run.relay_sends(b"x" * MAX_LINK_LINE_BYTES)  # no newline can still fit
        self.assertEqual(run.finish(), (4, "desk-link: oversize\n"))
        self.assertEqual(recv_all(run.hub), b"")

    def test_one_byte_over_the_cap_from_the_hub_exits_4_forwarding_nothing_of_it(self):
        run = PumpRun(self)
        sender = threading.Thread(target=lambda: _send_quietly(run.hub, line_of(MAX_LINK_LINE_BYTES + 1)),
                                  daemon=True)
        sender.start()
        self.assertEqual(run.finish(), (4, "desk-link: oversize\n"))
        self.assertEqual(run.relay_received(), b"")


def _send_quietly(conn: socket.socket, data: bytes) -> None:
    try:
        conn.sendall(data)
    except OSError:
        pass  # the pump closed its side after the cap


class PumpNoHubTest(unittest.TestCase):
    def test_no_hub_writes_the_version_free_refusal_and_exits_3(self):
        run = PumpRun(self, listen=False)
        self.assertEqual(run.finish(), (3, "desk-link: hub_not_running\n"))
        line = run.relay_received()
        kind, fields = decode_frame(line, Side.HUB, v=None)
        self.assertEqual(kind, LinkType.LINK_REFUSED)
        self.assertEqual(fields, {"reason": RefusedCode.HUB_NOT_RUNNING.value, "versions": list(LINK_VERSIONS)})
        self.assertNotIn(b'"v"', line)

    def test_every_exit_code_is_a_wire_shaped_code(self):
        codes = {outcome.code for outcome in PumpExit}
        self.assertEqual(len(codes), len(PumpExit))
        for code in codes:
            self.assertRegex(code, r"\A[a-z][a-z0-9_]{0,39}\Z")


class ForcedCommandTest(unittest.TestCase):
    """`bin/desk-link` as sshd runs it: `link_argv` (-I -B), the
    environment sshd gives (HOME), the hub's socket from DeskConfig."""

    def setUp(self):
        self.home = make_home(self)
        self.config = desk_config(self.home)
        self.argv = [sys.executable, "-I", "-B", str(REPO / "bin" / "desk-link")]
        self.env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def test_link_argv_runs_this_entry_point(self):
        self.assertEqual(Path(self.config.link_argv[-1]), REPO / "bin" / "desk-link")
        self.assertEqual(list(self.config.link_argv[1:3]), ["-I", "-B"])

    def test_no_hub(self):
        done = subprocess.run(self.argv, env=self.env, input=b"", capture_output=True, timeout=WAIT_SECONDS)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stderr, b"desk-link: hub_not_running\n")
        kind, _ = decode_frame(done.stdout, Side.HUB, v=None)
        self.assertEqual(kind, LinkType.LINK_REFUSED)

    def test_pumps_to_the_hubs_link_socket(self):
        self.config.runtime_dir.mkdir(parents=True, mode=0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.config.link_socket))
        listener.listen(1)
        child = subprocess.Popen(self.argv, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        self.addCleanup(child.kill)
        listener.settimeout(WAIT_SECONDS)
        hub, _ = listener.accept()
        self.addCleanup(hub.close)
        hub.settimeout(WAIT_SECONDS)
        hub.sendall(b'{"from":"hub"}\n')
        self.assertEqual(child.stdout.readline(), b'{"from":"hub"}\n')
        child.stdin.write(b'{"from":"relay"}\n')
        child.stdin.flush()
        self.assertEqual(hub.recv(65536), b'{"from":"relay"}\n')
        child.stdin.close()
        self.assertEqual(child.wait(WAIT_SECONDS), 0)
        self.assertEqual(child.stderr.read(), b"desk-link: relay_closed\n")
        self.assertEqual(recv_all(hub), b"")
        child.stdout.close()
        child.stderr.close()

if __name__ == "__main__":
    unittest.main()
