"""Bounded child processes for the desk link (`ssh-keygen`, `ssh -G`, the
relay's `ssh`): docs/voice-desk.md, "Processes, sockets and files".

Every child is spawned with `close_fds=True` (it holds no descriptor but
the ones named in `pass_fds`), an explicit environment, and no shell.
`run_bounded` reads its pipes to their caps under one total deadline. On
every exit path, normal or not, the child gets the cleanup: `SIGTERM`, up
to `TERM_GRACE_SECONDS` for it to exit, then `SIGKILL` and `waitpid`, so
none is left running or a zombie. The deadline and the cleanup are separate
bounds: the deadline ends the caller's wait (it gets its timeout then), and
the cleanup follows on its own thread, holding no lock of the caller's, so
a child outlives its deadline by at most the grace plus the time SIGKILL
takes to land. Standard library only.
"""

import contextlib
from dataclasses import dataclass
import os
import selectors
import signal
import subprocess
import threading
import time

TERM_GRACE_SECONDS = 2
# Payloads handed to a child through `data_pipes` are written before it
# starts, so each must fit a pipe's buffer (16 KiB on macOS by default).
MAX_PIPE_PAYLOAD_BYTES = 8 * 1024
_READ_CHUNK = 65536


class Child:
    """One spawned child and its cleanup. `cleanup()` is idempotent and may
    be called from any thread; `reaped` is set once `waitpid` is done."""

    def __init__(self, popen: subprocess.Popen):
        self.popen = popen
        self.pid = popen.pid
        self.reaped = threading.Event()
        self._lock = threading.Lock()
        self._cleaning = False

    def wait_until(self, deadline: float, clock=time.monotonic) -> int | None:
        """The exit status once it exits (reaped), or None at the deadline."""
        try:
            status = self.popen.wait(timeout=max(0.0, deadline - clock()))
        except subprocess.TimeoutExpired:
            return None
        self._close_pipes()
        self.reaped.set()
        return status

    def cleanup(self) -> None:
        """SIGTERM now, then on a separate thread up to TERM_GRACE_SECONDS for
        it to exit, then SIGKILL; `waitpid` in every case."""
        with self._lock:
            if self._cleaning or self.reaped.is_set():
                return
            self._cleaning = True
        if self.popen.poll() is not None:
            self._close_pipes()
            self.reaped.set()
            return
        with contextlib.suppress(ProcessLookupError):
            self.popen.send_signal(signal.SIGTERM)
        # Not a daemon: a short-lived command that returns right after a
        # timeout still waits, at interpreter exit, for the kill and waitpid.
        threading.Thread(target=self._reap, name=f"reap-{self.pid}").start()

    def _reap(self) -> None:
        try:
            self.popen.wait(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                self.popen.kill()
            self.popen.wait()
        finally:
            self._close_pipes()
            self.reaped.set()

    def _close_pipes(self) -> None:
        for stream in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()


def spawn(argv, *, env: dict, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
          pass_fds=()) -> Child:
    """Start `argv` (no shell) with exactly `env` and no inherited descriptor
    but `pass_fds`. OSError when it can't be started."""
    popen = subprocess.Popen(list(argv), env=dict(env), stdin=stdin, stdout=stdout, stderr=stderr,
                             close_fds=True, pass_fds=tuple(pass_fds))
    return Child(popen)


@dataclass(frozen=True)
class ChildResult:
    child: Child
    # None when the deadline passed first (`timed_out`).
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    # A stream that went past its cap: reading stopped there and our end
    # was closed, so its bytes are only the first `limit`.
    stdout_overflow: bool
    stderr_overflow: bool


def run_bounded(argv, *, env: dict, input: bytes | None = None, deadline_seconds: float,
                stdout_limit: int, stderr_limit: int, pass_fds=(), clock=time.monotonic) -> ChildResult:
    """Run `argv` to completion or to the deadline, whichever comes first.
    stdin is `input` (closed after it) or /dev/null. OSError when the child
    can't be started; any other exception still cleans the child up."""
    deadline = clock() + deadline_seconds
    child = spawn(argv, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                  pass_fds=pass_fds)
    try:
        streams = _exchange(child.popen, input, deadline, stdout_limit, stderr_limit, clock)
        returncode = child.wait_until(deadline, clock) if streams.finished else None
    finally:
        if not child.reaped.is_set():
            child.cleanup()
    return ChildResult(child, returncode, bytes(streams.out), bytes(streams.err), returncode is None,
                       streams.out_overflow, streams.err_overflow)


class _Streams:
    def __init__(self):
        self.out, self.err = bytearray(), bytearray()
        self.out_overflow = self.err_overflow = False
        self.finished = False


def _exchange(popen, payload, deadline, stdout_limit, stderr_limit, clock) -> _Streams:
    """Feed stdin and read stdout and stderr until both close or the deadline."""
    streams = _Streams()
    limits = {"out": stdout_limit, "err": stderr_limit}
    sent = 0
    with selectors.DefaultSelector() as selector:
        if payload is not None:
            os.set_blocking(popen.stdin.fileno(), False)
            selector.register(popen.stdin, selectors.EVENT_WRITE, "in")
        for stream, name in ((popen.stdout, "out"), (popen.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while any(key.data != "in" for key in selector.get_map().values()):
            remaining = deadline - clock()
            if remaining <= 0:
                return streams
            for key, _ in selector.select(remaining):
                if key.data == "in":
                    sent = _feed(selector, key.fileobj, payload, sent)
                    continue
                buffer = streams.out if key.data == "out" else streams.err
                try:
                    chunk = os.read(key.fileobj.fileno(), _READ_CHUNK)
                except BlockingIOError:
                    continue
                if chunk:
                    buffer.extend(chunk)
                if not chunk or len(buffer) > limits[key.data]:
                    if chunk:
                        del buffer[limits[key.data]:]
                        setattr(streams, f"{key.data}_overflow", True)
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
        for key in list(selector.get_map().values()):
            selector.unregister(key.fileobj)
            key.fileobj.close()
    streams.finished = True
    return streams


def _feed(selector, stream, payload: bytes, sent: int) -> int:
    try:
        sent += os.write(stream.fileno(), payload[sent:sent + _READ_CHUNK])
    except BlockingIOError:
        return sent
    except BrokenPipeError:
        sent = len(payload)
    if sent >= len(payload):
        selector.unregister(stream)
        stream.close()
    return sent


@contextlib.contextmanager
def data_pipes(*payloads: bytes):
    """Read ends of pipes, each already holding one payload with its write
    end closed, for a child to read as `/dev/fd/<n>` (pass them in
    `pass_fds`). Closed here on exit, whatever the child did."""
    fds: list[int] = []
    try:
        for payload in payloads:
            if len(payload) > MAX_PIPE_PAYLOAD_BYTES:
                raise ValueError("pipe payload too large")
            read_end, write_end = os.pipe()
            fds.append(read_end)
            try:
                os.set_blocking(write_end, False)
                if os.write(write_end, payload) != len(payload):
                    raise OSError("pipe payload not written whole")
            finally:
                os.close(write_end)
        yield fds
    finally:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)
