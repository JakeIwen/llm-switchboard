"""desk/link_writer.py: one outcome per frame, set once, by the rules of
docs/voice-desk.md ("Writers: one outcome per frame"). These are the
writer-only tests of the Tests table's "Writers and outcomes" row; the hub
and relay ones need those modules."""

import os
import socket
import threading
import time
import unittest

from llm_switchboard.desk.link_writer import (
    LinkWriter, PipeTarget, SocketTarget, SubmitRefusal, WriterStop,
)
from llm_switchboard.desk.model import WriteOutcome

ALLOWANCE = 10.0
FRAME = b'{"type":"event"}\n'


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.now

    def advance(self, seconds):
        with self._lock:
            self.now += seconds


class ScriptedTarget:
    """A target whose `send` runs a script step per call: each step returns
    how many bytes the kernel accepts, or raises. `log` records events in
    order, so a test can check shutdown came before the outcome."""

    def __init__(self, steps, log=None):
        self.steps = list(steps)
        self.log = log if log is not None else []
        self.sent = bytearray()
        self.closed_by = None
        self._shut = False

    def send(self, view):
        if self._shut:
            raise BrokenPipeError("shut")
        step = self.steps.pop(0) if self.steps else (lambda v: len(v))
        accepted = step(view)
        self.sent.extend(view[:accepted])
        self.log.append(("send", accepted))
        return accepted

    def wait_writable(self, timeout, wake_fd):
        time.sleep(min(timeout, 0.01))

    def shutdown(self):
        self._shut = True
        self.log.append(("shutdown",))

    def close(self):
        self.closed_by = threading.current_thread().name
        self.log.append(("close",))


def full_then(fn):
    def step(view):
        return fn(view)
    return step


def blocked(view):
    raise BlockingIOError


class WriterCase(unittest.TestCase):
    def writer(self, target, **options):
        options.setdefault("max_frames", 64)
        options.setdefault("max_bytes", 1 << 20)
        options.setdefault("deadline_seconds", 5)
        failures = []
        writer = LinkWriter(target, on_failure=failures.append, name="writer-under-test", **options)
        writer.failures = failures
        writer.start()
        self.addCleanup(lambda: (writer.retire(), writer.join(ALLOWANCE)))
        return writer

    def socket_pair(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        return ours, theirs


class OutcomeTest(WriterCase):
    def test_whole_frame_written(self):
        ours, theirs = self.socket_pair()
        writer = self.writer(SocketTarget(ours))
        frame = writer.submit(FRAME)
        self.assertIs(frame.wait(ALLOWANCE), WriteOutcome.WRITTEN)
        self.assertEqual(theirs.recv(100), FRAME)

    def test_writer_decides_after_final_byte_accepted(self):
        # The deadline passes while the kernel takes the final bytes, before
        # the writer records them: the counts decide, so it is written.
        clock = FakeClock()

        def accept_all_then_jump(view):
            clock.advance(60)
            return len(view)
        target = ScriptedTarget([lambda v: 5, accept_all_then_jump])
        writer = self.writer(target, clock=clock)
        frame = writer.submit(FRAME)
        self.assertIs(frame.wait(ALLOWANCE), WriteOutcome.WRITTEN)
        self.assertEqual(bytes(target.sent), FRAME)
        self.assertNotIn(("shutdown",), target.log[:2])

    def test_partial_frame_is_unfinished(self):
        clock = FakeClock()

        def some_then_full(view):
            target.steps[:] = [blocked] * 10_000
            clock.advance(6)  # the deadline passes while the socket stays full
            return 4
        log = []
        target = ScriptedTarget([some_then_full], log)
        writer = self.writer(target, clock=clock)
        frame = writer.submit(FRAME)
        outcome = frame.wait(ALLOWANCE)
        log.append(("outcome", outcome))
        self.assertIs(outcome, WriteOutcome.UNFINISHED)
        self.assertTrue(frame.begun)
        self.assertEqual(frame.accepted, 4)
        # Shut down before the decision, so no later byte completes it.
        self.assertLess(log.index(("shutdown",)), log.index(("outcome", outcome)))
        self.assertTrue(writer.join(ALLOWANCE))
        self.assertEqual(writer.failures, [WriterStop.DEADLINE])

    def test_closed_after_begun_mark_before_first_write_is_unfinished(self):
        with self.subTest("peer closed"):
            ours, theirs = self.socket_pair()
            theirs.close()
            writer = self.writer(SocketTarget(ours))
            frame = writer.submit(FRAME)
            self.assertIs(frame.wait(ALLOWANCE), WriteOutcome.UNFINISHED)
            self.assertEqual((frame.begun, frame.accepted), (True, 0))
            self.assertTrue(writer.join(ALLOWANCE))
            self.assertEqual(writer.failures, [WriterStop.WRITE_FAILED])
        with self.subTest("retired from another thread"):
            holder = {}

            def retire_first(view):
                retirer = threading.Thread(target=holder["writer"].retire)
                retirer.start()
                retirer.join()
                raise BrokenPipeError("shut down by the retirement")
            target = ScriptedTarget([retire_first])
            holder["writer"] = writer = self.writer(target)
            frame = writer.submit(FRAME)
            self.assertIs(frame.wait(ALLOWANCE), WriteOutcome.UNFINISHED)
            self.assertEqual((frame.begun, frame.accepted), (True, 0))
            self.assertTrue(writer.join(ALLOWANCE))
            self.assertEqual(writer.failures, [], "a retirement is its caller's to report")
        with self.subTest("socket full from the begun mark to the deadline"):
            ours, theirs = self.socket_pair()
            ours.setblocking(False)
            try:
                while True:
                    ours.send(b"x" * 65536)
            except BlockingIOError:
                pass
            writer = self.writer(SocketTarget(ours), deadline_seconds=0.5)
            frame = writer.submit(FRAME)
            self.assertIs(frame.wait(ALLOWANCE), WriteOutcome.UNFINISHED)
            self.assertEqual((frame.begun, frame.accepted), (True, 0))
            self.assertTrue(writer.join(ALLOWANCE))
            self.assertEqual(writer.failures, [WriterStop.DEADLINE])

    def test_unbegun_frames_not_begun_on_retire(self):
        release = threading.Event()

        def hold(view):
            release.wait(ALLOWANCE)
            raise BrokenPipeError("retired")
        target = ScriptedTarget([hold])
        writer = self.writer(target)
        first = writer.submit(FRAME)
        queued = [writer.submit(FRAME) for _ in range(3)]
        _wait_for(lambda: first.begun)
        writer.retire()
        for frame in queued:
            self.assertIs(frame.wait(ALLOWANCE), WriteOutcome.NOT_BEGUN)
            self.assertFalse(frame.begun)
        release.set()
        # The frame being written gets its outcome from the writer, never not_begun.
        self.assertIs(first.wait(ALLOWANCE), WriteOutcome.UNFINISHED)
        late = writer.submit(FRAME)
        self.assertEqual((late.outcome, late.refusal), (WriteOutcome.NOT_BEGUN, SubmitRefusal.RETIRED))

    def test_queued_frame_past_its_deadline_is_not_begun_and_stops_the_writer(self):
        clock = FakeClock()
        release = threading.Event()

        def slow(view):
            release.wait(ALLOWANCE)
            return len(view)
        writer = self.writer(ScriptedTarget([slow]), clock=clock)
        first = writer.submit(FRAME)
        second = writer.submit(FRAME)
        _wait_for(lambda: first.begun)
        clock.advance(6)  # the second's deadline passes while it waits in the queue
        release.set()
        self.assertIs(first.wait(ALLOWANCE), WriteOutcome.WRITTEN)
        self.assertIs(second.wait(ALLOWANCE), WriteOutcome.NOT_BEGUN)
        self.assertFalse(second.begun)
        self.assertTrue(writer.join(ALLOWANCE))
        self.assertEqual(writer.failures, [WriterStop.DEADLINE])

    def test_writer_never_begins_a_frame_with_an_outcome(self):
        release = threading.Event()

        def slow(view):
            release.wait(ALLOWANCE)
            return len(view)
        target = ScriptedTarget([slow])
        writer = self.writer(target)
        first = writer.submit(b"first\n")
        second = writer.submit(b"second\n")
        _wait_for(lambda: first.begun)
        # The second's waiter gives up before the writer reaches it.
        self.assertIs(second.wait(0.01), WriteOutcome.UNFINISHED)
        release.set()
        self.assertIs(first.wait(ALLOWANCE), WriteOutcome.WRITTEN)
        third = writer.submit(b"third\n")
        self.assertIs(third.wait(ALLOWANCE), WriteOutcome.WRITTEN)
        self.assertFalse(second.begun)
        self.assertEqual(bytes(target.sent), b"first\nthird\n")

    def test_waiter_guard_can_only_set_unfinished(self):
        release = threading.Event()

        def slow(view):
            release.wait(ALLOWANCE)
            return len(view)
        writer = self.writer(ScriptedTarget([slow]))
        frame = writer.submit(FRAME)
        _wait_for(lambda: frame.begun)
        self.assertIs(frame.wait(0.01), WriteOutcome.UNFINISHED)
        release.set()
        later = writer.submit(FRAME)
        self.assertIs(later.wait(ALLOWANCE), WriteOutcome.WRITTEN)
        # The cell was set once: the writer's own WRITTEN didn't replace it.
        self.assertIs(frame.outcome, WriteOutcome.UNFINISHED)
        # And a guard on a decided frame changes nothing.
        self.assertIs(later.wait(0), WriteOutcome.WRITTEN)

    def test_frame_and_byte_caps_refuse_at_submit(self):
        release = threading.Event()

        def slow(view):
            release.wait(ALLOWANCE)
            return len(view)
        writer = self.writer(ScriptedTarget([slow]), max_frames=3, max_bytes=len(FRAME) * 10)
        frames = [writer.submit(FRAME) for _ in range(3)]
        over = writer.submit(FRAME)
        self.assertEqual((over.outcome, over.refusal, over.begun), (WriteOutcome.NOT_BEGUN, SubmitRefusal.FULL, False))
        release.set()
        for frame in frames:
            self.assertIs(frame.wait(ALLOWANCE), WriteOutcome.WRITTEN)
        big = writer.submit(b"x" * (len(FRAME) * 10 + 1))
        self.assertEqual(big.refusal, SubmitRefusal.FULL)
        self.assertIs(writer.submit(FRAME).wait(ALLOWANCE), WriteOutcome.WRITTEN)

    def test_retire_from_other_thread_shuts_down_writer_closes(self):
        ours, theirs = self.socket_pair()
        writer = self.writer(SocketTarget(ours))
        self.assertIs(writer.submit(FRAME).wait(ALLOWANCE), WriteOutcome.WRITTEN)
        retirer = threading.Thread(target=writer.retire, name="retirer")
        retirer.start()
        retirer.join(ALLOWANCE)
        theirs.settimeout(ALLOWANCE)
        self.assertEqual(theirs.recv(100), FRAME)
        self.assertEqual(theirs.recv(100), b"")  # shut down: the peer sees EOF
        self.assertTrue(writer.join(ALLOWANCE))
        # The owner's own socket object is untouched; only the writer's copy closed.
        self.assertGreaterEqual(ours.fileno(), 0)
        with self.subTest("only the writer thread closes the descriptor"):
            target = ScriptedTarget([])
            writer = self.writer(target)
            closer = threading.Thread(target=writer.retire, name="retirer")
            closer.start()
            closer.join(ALLOWANCE)
            self.assertTrue(writer.join(ALLOWANCE))
            self.assertEqual(target.closed_by, "writer-under-test")
            self.assertLess(target.log.index(("shutdown",)), target.log.index(("close",)))

    def test_pipe_target_written_and_unfinished_on_closed_reader(self):
        read_end, write_end = os.pipe()
        self.addCleanup(lambda: _close_quietly(read_end))
        writer = self.writer(PipeTarget(write_end))
        self.assertIs(writer.submit(FRAME).wait(ALLOWANCE), WriteOutcome.WRITTEN)
        self.assertEqual(os.read(read_end, 100), FRAME)
        os.close(read_end)
        frame = writer.submit(FRAME)
        self.assertIs(frame.wait(ALLOWANCE), WriteOutcome.UNFINISHED)
        self.assertTrue(writer.join(ALLOWANCE))
        self.assertEqual(writer.failures, [WriterStop.WRITE_FAILED])


def _wait_for(predicate):
    deadline = time.monotonic() + ALLOWANCE
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition never held")
        time.sleep(0.005)


def _close_quietly(fd):
    try:
        os.close(fd)
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main()
