"""The one-thread writer for the desk link (docs/voice-desk.md, "Writers:
one outcome per frame"), used for the hub's link socket and the relay's
ssh pipe and channel sockets.

One thread owns the target and writes it non-blocking, enforcing each
frame's deadline (counted from `submit`, so queue waits count) itself.
Every submitted frame has one outcome cell, set once under the writer's
lock (`model.WriteOutcome`):

- NOT_BEGUN only on positive evidence that the frame was never marked
  begun: refused at submit (queue full, writer retired), its deadline
  passed while it waited, or the writer retired first. The writer marks a
  frame begun, under the same lock, before it hands the kernel any byte,
  and never begins one whose outcome is already set.
- WRITTEN by the writer only, when the kernel accepted every byte, the
  final newline included, whatever the clock says by then.
- UNFINISHED for every frame marked begun and not completed, zero bytes
  included, or by a waiter whose guard ran out (`Frame.wait`), which can
  set nothing else.

When a deadline passes or a write fails after the begun mark, the writer
shuts the target down before it sets the outcome, so no later byte can
complete the frame, then stops: every queued frame becomes NOT_BEGUN and
`on_failure` is told once. `retire()` from another thread shuts the target
down and wakes the writer; only the writer thread closes the target's
descriptor. Once a frame's outcome is set, neither the frame nor the writer
keeps its bytes. Standard library only.
"""

from collections import deque
import contextlib
from enum import StrEnum
import os
import select
import socket
import threading
import time
from typing import Callable

from .model import WriteOutcome

# Upper bound on one wait for the target, so the writer rereads its clock
# (and a retirement) at least this often.
_POLL_SLICE_SECONDS = 0.05


class WriterStop(StrEnum):
    """Why a writer stopped by itself (passed to `on_failure`)."""
    DEADLINE = "deadline"
    WRITE_FAILED = "write_failed"


class SubmitRefusal(StrEnum):
    """Why `submit` returned a frame that never entered the queue."""
    FULL = "full"
    RETIRED = "retired"


class Frame:
    """One frame and its outcome cell. `outcome` is None until set; after
    that it never changes. `data` is emptied when the outcome is set: a
    caller may keep the frame for its outcome long after (the relay keeps a
    channel's events), and the bytes, a prompt among them, are no longer
    needed by anyone."""

    def __init__(self, writer: "LinkWriter", data: bytes, deadline: float):
        self._writer = writer
        self.data = data
        self.size = len(data)
        self.deadline = deadline
        self.begun = False
        self.outcome: WriteOutcome | None = None
        # Bytes the kernel accepted, as `send` reported them.
        self.accepted = 0
        # Set when `submit` refused the frame (it was never queued).
        self.refusal: SubmitRefusal | None = None
        self._done = threading.Event()

    def wait(self, guard_seconds: float) -> WriteOutcome:
        """The outcome, waiting at most `guard_seconds`. A guard that runs
        out (a writer bug) sets UNFINISHED, and can set nothing else."""
        if not self._done.wait(guard_seconds):
            self._writer._settle(self, WriteOutcome.UNFINISHED)
        return self.outcome


class SocketTarget:
    """A connected stream socket, written through a private duplicate of its
    descriptor with MSG_DONTWAIT, so the owner's own socket object (its
    reader's timeout mode included) is never changed. `shutdown` ends the
    connection both ways, which also ends the owner's reads."""

    def __init__(self, sock: socket.socket):
        self._sock = socket.socket(fileno=os.dup(sock.fileno()))
        self._lock = threading.Lock()
        self._shut = False

    def send(self, view: memoryview) -> int:
        with self._lock:
            if self._shut:
                raise BrokenPipeError("target shut down")
            return self._sock.send(view, socket.MSG_DONTWAIT)

    def wait_writable(self, timeout: float, wake_fd: int) -> None:
        poller = select.poll()
        poller.register(self._sock.fileno(), select.POLLOUT)
        poller.register(wake_fd, select.POLLIN)
        poller.poll(max(0, int(timeout * 1000)))

    def shutdown(self) -> None:
        with self._lock:
            self._shut = True
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        with self._lock:
            self._shut = True
            self._sock.close()


class PipeTarget:
    """The write end of a pipe (the relay's ssh stdin), owned by the writer.
    A pipe can't be shut down from another thread without racing the
    writer, so `shutdown` refuses every later write and the writer closes
    the descriptor."""

    def __init__(self, fd: int):
        os.set_blocking(fd, False)
        self._fd = fd
        self._lock = threading.Lock()
        self._shut = False

    def send(self, view: memoryview) -> int:
        with self._lock:
            if self._shut:
                raise BrokenPipeError("target shut down")
            return os.write(self._fd, view)

    def wait_writable(self, timeout: float, wake_fd: int) -> None:
        poller = select.poll()
        poller.register(self._fd, select.POLLOUT)
        poller.register(wake_fd, select.POLLIN)
        poller.poll(max(0, int(timeout * 1000)))

    def shutdown(self) -> None:
        with self._lock:
            self._shut = True

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                self._shut = True
                os.close(self._fd)
                self._fd = -1


class LinkWriter:
    def __init__(self, target, *, max_frames: int, max_bytes: int | None = None, deadline_seconds: float,
                 clock: Callable[[], float] = time.monotonic,
                 on_failure: Callable[[WriterStop], None] | None = None, name: str = "link-writer"):
        self._target = target
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._deadline_seconds = deadline_seconds
        self._clock = clock
        self._on_failure = on_failure
        self._name = name
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._queue: deque[Frame] = deque()
        # Frames without an outcome yet (queued or being written), and their bytes.
        self._open_frames = 0
        self._open_bytes = 0
        self._retired = False
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    # -- any thread ------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    @property
    def retired(self) -> bool:
        with self._lock:
            return self._retired

    def submit(self, data: bytes, deadline_seconds: float | None = None) -> Frame:
        """Queue one whole frame. A frame that can't be queued comes back
        with `refusal` set and its outcome already NOT_BEGUN; the caller
        decides what that means (the queue rules differ per writer)."""
        seconds = self._deadline_seconds if deadline_seconds is None else deadline_seconds
        frame = Frame(self, bytes(data), self._clock() + seconds)
        with self._lock:
            if self._retired:
                frame.refusal = SubmitRefusal.RETIRED
            elif self._open_frames >= self._max_frames or (
                    self._max_bytes is not None and self._open_bytes + frame.size > self._max_bytes):
                frame.refusal = SubmitRefusal.FULL
            if frame.refusal is not None:
                frame.outcome = WriteOutcome.NOT_BEGUN
                frame.data = b""
                frame._done.set()
                return frame
            self._queue.append(frame)
            self._open_frames += 1
            self._open_bytes += frame.size
            self._changed.notify_all()
        return frame

    def retire(self) -> None:
        """Stop the writer: every queued frame not marked begun becomes
        NOT_BEGUN, the target is shut down, and a frame being written gets
        its outcome from the writer. Idempotent; never waits."""
        with self._lock:
            if self._retired:
                return
            self._retired = True
            self._abandon_queue()
            self._changed.notify_all()
            self._wake()
        self._target.shutdown()

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # -- the writer thread -------------------------------------------------------

    def _run(self) -> None:
        stop: WriterStop | None = None
        try:
            while True:
                frame, data, stop = self._next_frame()
                if frame is None:
                    break
                outcome = self._write(frame, data)
                data = b""  # hold no frame's bytes while waiting for the next
                if outcome is not WriteOutcome.WRITTEN:
                    # A retirement asked for elsewhere is its caller's to report.
                    if not self.retired:
                        stop = WriterStop.DEADLINE if self._clock() >= frame.deadline else WriterStop.WRITE_FAILED
                    break
        finally:
            with self._lock:
                self._retired = True
                self._abandon_queue()
                with contextlib.suppress(OSError):
                    os.close(self._wake_r)
                with contextlib.suppress(OSError):
                    os.close(self._wake_w)
                self._wake_r = self._wake_w = -1
            self._target.shutdown()
            self._target.close()
        if stop is not None and self._on_failure is not None:
            self._on_failure(stop)

    def _next_frame(self) -> tuple[Frame | None, bytes, WriterStop | None]:
        """The next frame to write, marked begun, with its bytes taken under
        the lock: a waiter's guard may set its outcome (and empty `data`)
        while it is being written, and the writer still decides from what
        it wrote of the whole frame."""
        with self._lock:
            while True:
                if self._retired:
                    return None, b"", None
                if not self._queue:
                    self._changed.wait()
                    continue
                frame = self._queue.popleft()
                if frame.outcome is not None:
                    continue  # a guard gave up on it: never begin it
                if self._clock() >= frame.deadline:
                    self._settle_locked(frame, WriteOutcome.NOT_BEGUN)
                    return None, b"", WriterStop.DEADLINE
                frame.begun = True
                return frame, frame.data, None

    def _write(self, frame: Frame, data: bytes) -> WriteOutcome:
        view = memoryview(data)
        while frame.accepted < len(view):
            if self.retired or self._clock() >= frame.deadline:
                break
            try:
                frame.accepted += self._target.send(view[frame.accepted:])
                continue
            except (BlockingIOError, InterruptedError):
                pass
            except OSError:
                break
            remaining = frame.deadline - self._clock()
            self._target.wait_writable(min(max(remaining, 0), _POLL_SLICE_SECONDS), self._wake_r)
            self._drain_wake()
        complete = frame.accepted == len(view)
        if not complete:
            # Shut down before deciding, so no later byte can complete it.
            self._target.shutdown()
        decided = WriteOutcome.WRITTEN if complete else WriteOutcome.UNFINISHED
        # A waiter's guard may have set unfinished already; the cell keeps it,
        # but whether the writer goes on depends on what it did itself.
        self._settle(frame, decided)
        return decided

    # -- the outcome cells ---------------------------------------------------------

    def _settle(self, frame: Frame, outcome: WriteOutcome) -> None:
        with self._lock:
            self._settle_locked(frame, outcome)

    def _settle_locked(self, frame: Frame, outcome: WriteOutcome) -> None:
        if frame.outcome is not None:
            return
        if outcome is WriteOutcome.NOT_BEGUN and frame.begun:
            raise AssertionError("a frame marked begun is never not_begun")
        frame.outcome = outcome
        self._open_frames -= 1
        self._open_bytes -= frame.size
        frame.data = b""
        frame._done.set()

    def _abandon_queue(self) -> None:
        while self._queue:
            frame = self._queue.popleft()
            if not frame.begun:
                self._settle_locked(frame, WriteOutcome.NOT_BEGUN)

    def _wake(self) -> None:
        if self._wake_w >= 0:
            with contextlib.suppress(BlockingIOError, OSError):
                os.write(self._wake_w, b"x")

    def _drain_wake(self) -> None:
        with self._lock:
            fd = self._wake_r
            if fd < 0:
                return
            with contextlib.suppress(BlockingIOError, OSError):
                while os.read(fd, 64):
                    pass
