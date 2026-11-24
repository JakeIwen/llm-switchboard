"""The relay's channel socket (relay_channels) with the Mac kernel faked
behind peer_mac's reader, a fake hub end (`FakeUplink`), raw channel
clients and the real `channel.HubLink`."""

import io
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

from desk_fixtures import SESSION_A, SESSION_B, ChannelClient, request_id, wait_until
from relay_fixture import EPOCH, FakeUplink, LinkFrameError, MacHome
from llm_switchboard.desk import channel, protocol
from llm_switchboard.desk.channel import HubLink, StdoutWriter, UndeliverableReason
from llm_switchboard.desk.link_protocol import (
    HEARTBEAT_SECONDS, IDLE_TIMEOUT_SECONDS, LINK_CODES, LinkType, UndeliverableCode, UnregisteredCode,
)
from llm_switchboard.desk.link_writer import SocketTarget
from llm_switchboard.desk.model import ReplyRefusal
from llm_switchboard.desk.operator import bind_private_socket
from llm_switchboard.desk.protocol import MessageType
from llm_switchboard.desk.relay_channels import REGISTER_REFUSAL_TEXT, RelayChannels, RelayRefusal
from llm_switchboard.desk.session_identity import read_session_file

GENEROUS = 15.0


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


class GatedTarget(SocketTarget):
    """The real channel socket target, with a byte budget a test sets: None
    passes everything; a number lets that many more bytes through, then the
    kernel reports the socket full. `before_send` runs on each send attempt
    (the writer has marked its frame begun by then)."""

    def __init__(self, sock):
        super().__init__(sock)
        self.budget = None
        self.attempts = 0
        self.before_send = None
        self.closed_by = None
        self.log = []

    def send(self, view):
        self.attempts += 1
        if self.before_send is not None:
            hook, self.before_send = self.before_send, None
            hook()
        if self.budget is None:
            return super().send(view)
        if self.budget <= 0:
            raise BlockingIOError
        sent = super().send(view[:self.budget])
        self.budget -= sent
        return sent

    def wait_writable(self, timeout, wake_fd):
        if self.budget is not None and self.budget <= 0:
            time.sleep(min(timeout, 0.01))
        else:
            super().wait_writable(timeout, wake_fd)

    def shutdown(self):
        self.log.append("shutdown")
        super().shutdown()

    def close(self):
        self.log.append("close")
        self.closed_by = threading.current_thread().name
        super().close()


class RelayCase(unittest.TestCase):
    def setUp(self):
        self.mac = MacHome(self)
        self.config = self.mac.config
        self.targets: list[GatedTarget] = []

    def start_relay(self, *, answer="registered", link=True, **options):
        def target(sock):
            gated = GatedTarget(sock)
            self.targets.append(gated)
            return gated
        options.setdefault("target_factory", target)
        relay = RelayChannels(self.config, reader=self.mac.kernel, **options)
        relay.start()
        self.addCleanup(relay.close)
        uplink = FakeUplink(relay, answer=answer)
        if link:
            relay.link_up(uplink)
        self.relay, self.uplink = relay, uplink
        return relay, uplink

    def client(self, session_id=SESSION_A) -> ChannelClient:
        client = ChannelClient(self.config.channel_socket, session_id)
        self.addCleanup(client.close)
        return client

    def assert_no_answer(self):
        """A connection the relay closes without a word (another user, or no room at accept)."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.settimeout(GENEROUS)
        sock.connect(str(self.config.channel_socket))
        try:
            sock.sendall(protocol.encode(MessageType.HELLO, session_id=SESSION_A))
            data = sock.recv(65536)
        except OSError:
            data = b""
        self.assertEqual(data, b"")

    def welcomed(self) -> ChannelClient:
        client = self.client()
        self.assertEqual(client.first[0], MessageType.WELCOME, client.first)
        return client

    def event(self, n: int, content: str = "hello", chan: int = 1) -> str:
        rid = request_id(n)
        self.uplink.send(LinkType.EVENT, chan=chan, request_id=rid, content=content)
        return rid

    def unregistered(self) -> dict:
        return self.uplink.wait_for(LinkType.UNREGISTERED, timeout=GENEROUS)

    def undeliverable_ids(self) -> dict:
        return {f["request_id"]: f["reason"] for f in self.uplink.of(LinkType.UNDELIVERABLE)}

    def assert_reports_before_unregistered(self):
        kinds = [k for k, _ in self.uplink.frames]
        last_undeliverable = max((i for i, k in enumerate(kinds) if k is LinkType.UNDELIVERABLE), default=-1)
        self.assertLess(last_undeliverable, kinds.index(LinkType.UNREGISTERED))

    def start_hub_link(self, *, writer=None, **options) -> HubLink:
        link = HubLink(self.config, SESSION_A, writer or StdoutWriter(io.BytesIO()), **options)
        link.start()
        return link


class AdmissionTest(RelayCase):
    def test_registered_channel_gets_welcome_with_link_epoch_and_register_fields(self):
        self.start_relay()
        client = self.welcomed()
        self.assertEqual(client.first[1]["epoch"], EPOCH)
        self.assertEqual(client.first[1]["generation"], 1)
        register = self.uplink.of(LinkType.REGISTER)[0]
        self.assertEqual((register["chan"], register["session_id"], register["claude_pid"]), (1, SESSION_A, 42))
        self.assertEqual((register["label"], register["cwd"]), ("desk work", "/Users/me/dev/project"))
        self.assertEqual(register["channel_start"], str(self.mac.child.stamp.micros))

    def test_hello_refused_while_link_down_or_versions_mismatch(self):
        relay, _ = self.start_relay(link=False)
        self.assertEqual(self.client().first, (MessageType.REFUSED, mock.ANY))
        self.assertEqual(self.client().first[1]["reason"], RelayRefusal.LINK_DOWN)
        relay.link_down(None, RelayRefusal.LINK_VERSION)
        self.assertEqual(self.client().first[1]["reason"], "desk link version mismatch")

    def test_other_uid_gets_no_answer(self):
        self.start_relay()
        self.mac.kernel.cred = (43, os.getuid() + 1)
        self.assert_no_answer()
        self.assertEqual(self.uplink.frames, [])

    def test_failed_mac_check_refused_without_register(self):
        self.start_relay()
        self.mac.publish(sessionId=SESSION_B)
        kind, message = self.client().first
        self.assertEqual((kind, message["reason"]), (MessageType.REFUSED, "session file does not match"))
        self.assertEqual(self.uplink.frames, [])

    def test_hub_refusal_codes_become_hub_text(self):
        for code, text in REGISTER_REFUSAL_TEXT.items():
            with self.subTest(code=code):
                self.mac = MacHome(self)
                self.config = self.mac.config
                self.start_relay(answer=code.value)
                self.assertEqual(self.client().first[1]["reason"], text)
        self.assertEqual(set(REGISTER_REFUSAL_TEXT), LINK_CODES[LinkType.REGISTER_REFUSED])

    def test_not_enrolled_refused_locally_for_25s(self):
        clock = FakeClock()
        self.start_relay(answer="not_enrolled", clock=clock)
        for _ in range(2):
            self.assertEqual(self.client().first[1]["reason"], "session is not enrolled")
        self.assertEqual(len(self.uplink.of(LinkType.REGISTER)), 1)
        clock.advance(25)
        self.client()
        self.assertEqual(len(self.uplink.of(LinkType.REGISTER)), 2)

    def test_register_unanswered_refused_slow_then_late_registered_unregistered(self):
        self.start_relay(answer=None, register_wait_seconds=0.2)
        self.assertEqual(self.client().first[1]["reason"], "desk link slow")
        self.uplink.send(LinkType.REGISTERED, chan=1, generation=7)
        self.assertEqual(self.unregistered(), {"chan": 1, "reason": "channel_closed"})

    def test_register_refused_busy_when_link_queue_full(self):
        self.start_relay()
        self.uplink.full = True
        self.assertEqual(self.client().first[1]["reason"], "busy")
        self.assertFalse(self.uplink.retired)

    def test_chan_never_issued_closes_link(self):
        self.start_relay()
        self.welcomed()
        for kind, fields in [(LinkType.EVENT, {"request_id": request_id(1), "content": "x"}),
                             (LinkType.REGISTERED, {"generation": 1}), (LinkType.DROP, {"reason": "unenrolled"})]:
            with self.subTest(kind=kind), self.assertRaises(LinkFrameError):
                self.uplink.send(kind, chan=2, **fields)


    def test_chan_never_recurs_across_links(self):
        # `chan` comes from one counter for the relay process's whole life,
        # so a link that replaces another never reuses a number, and a frame
        # on the new link naming an old link's `chan` reaches no channel.
        relay, uplink = self.start_relay()
        chans = []
        for n in range(3):
            if n:
                relay.link_down(uplink)
                self.assertEqual(client.read(), (None, {}))
                uplink = self.uplink = FakeUplink(relay)
                relay.link_up(uplink)
            client = self.welcomed()
            chans += [f["chan"] for f in uplink.of(LinkType.REGISTER)]
        self.assertEqual(chans, [1, 2, 3])
        rid = self.event(1, chan=1)
        self.assertEqual(uplink.wait_for(LinkType.UNDELIVERABLE),
                         {"chan": 1, "request_id": rid, "reason": "channel_closed"})
        self.assertFalse(uplink.retired)
        client.send(MessageType.PING)
        self.assertEqual(client.read()[0], MessageType.PONG)

    def connect_together(self, count: int) -> list[ChannelClient]:
        """`count` channels connect at once, as every live channel does after a link reconnect."""
        start = threading.Barrier(count)
        clients: list[ChannelClient | Exception] = [None] * count

        def connect(i):
            start.wait(GENEROUS)
            try:
                clients[i] = self.client()
            except Exception as exc:  # reported below, on the test's thread
                clients[i] = exc
        threads = [threading.Thread(target=connect, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(GENEROUS)
        for client in clients:
            if isinstance(client, Exception):
                raise client
            self.assertEqual(client.first[0], MessageType.WELCOME, client.first)
        return clients

    def test_concurrent_registers_reach_the_link_in_chan_order(self):
        # The hub takes a `chan` below one it has seen on the link as reused
        # and drops the link, so `register` frames go out in `chan` order.
        # Each round is one link: every channel registers again at once.
        relay, _ = self.start_relay()
        for round_ in range(10):
            uplink = self.uplink
            # 5 a round: two rounds still closing leave room under the 16-channel cap.
            clients = self.connect_together(5)
            chans = [f["chan"] for f in uplink.of(LinkType.REGISTER)]
            self.assertEqual(chans, sorted(set(chans)), f"round {round_}")
            self.assertEqual(len(chans), 5)
            relay.link_down(uplink)
            for client in clients:
                self.assertEqual(client.read(), (None, {}))
                client.close()
            self.uplink = FakeUplink(relay)
            relay.link_up(self.uplink)

    def test_registers_in_chan_order_whatever_order_admissions_finish(self):
        # The work before a `register` (the Mac check, the session file) can
        # finish in any order across channels; held back here and released
        # last-first, each after the one before it has registered.
        self.start_relay()
        count = 5
        held = ReverseRelease(count, lambda: len(self.uplink.of(LinkType.REGISTER)))
        with mock.patch("llm_switchboard.desk.relay_channels.read_session_file", held):
            self.connect_together(count)
        chans = [f["chan"] for f in self.uplink.of(LinkType.REGISTER)]
        self.assertEqual(chans, list(range(1, count + 1)))
        self.assertEqual(held.released, list(reversed(range(count))))


class ReverseRelease:
    """Wraps `read_session_file`: holds its first `count` callers until all
    have arrived, then lets them return last-first, each once `registered()`
    shows the one before it has put its `register` on the link."""

    def __init__(self, count: int, registered):
        self.count = count
        self.registered = registered
        self.released: list[int] = []
        self._arrived = 0
        self._changed = threading.Condition()

    def __call__(self, *args, **kwargs):
        with self._changed:
            me = self._arrived
            self._arrived += 1
            self._changed.notify_all()
            if not self._changed.wait_for(lambda: self._arrived >= self.count, GENEROUS):
                raise AssertionError("not every channel reached the session file read")
        turn = self.count - 1 - me
        wait_until(lambda: self.registered() >= turn, GENEROUS)
        with self._changed:
            self.released.append(me)
        return read_session_file(*args, **kwargs)


class ChannelFramesTest(RelayCase):
    def test_relay_answers_channel_ping_locally(self):
        self.start_relay()
        client = self.welcomed()
        client.send(MessageType.PING)
        self.assertEqual(client.read()[0], MessageType.PONG)
        self.assertEqual([k for k, _ in self.uplink.frames], [LinkType.REGISTER])

    def test_event_delivered_and_reply_round_trip(self):
        self.start_relay()
        client = self.welcomed()
        rid = self.event(1, "prompt")
        self.assertEqual(client.read(), (MessageType.EVENT, mock.ANY))
        client.send(MessageType.DELIVERED, request_id=rid)
        self.assertEqual(self.uplink.wait_for(LinkType.DELIVERED), {"chan": 1, "request_id": rid})
        client.send(MessageType.REPLY, request_id=rid, text="answer", spoken="said")
        self.uplink.wait_for(LinkType.REPLY)
        self.uplink.send(LinkType.REPLY_RESULT, chan=1, request_id=rid, ok=False, reason="not_outstanding")
        kind, message = client.read()
        self.assertEqual((kind, message["ok"], message["reason"]), (MessageType.REPLY_RESULT, False,
                                                                    ReplyRefusal.NOT_OUTSTANDING.value))
        client.send(MessageType.REPLY, request_id=rid, text="again", spoken="said")
        self.uplink.wait_for(LinkType.REPLY, lambda f: f["text"] == "again")
        self.uplink.send(LinkType.REPLY_RESULT, chan=1, request_id=rid, ok=True, reason=None)
        kind, message = client.read()
        self.assertEqual((message["ok"], message["reason"]), (True, None))
        self.uplink.send(LinkType.REPLY_RESULT, chan=1, request_id=rid, ok=True, reason=None)  # unsolicited
        self.assertEqual(self.relay.late["reply_result"], 1)

    def test_bad_or_unsolicited_request_id_refused_at_relay(self):
        self.start_relay()
        client = self.welcomed()
        rids = [self.event(n) for n in range(1, 10)]
        for _ in rids:
            client.read()
        for bad in ["not-an-id", request_id(99), rids[0], 7]:
            with self.subTest(request_id=bad):
                client.send(MessageType.REPLY, request_id=bad, text="secret words", spoken="s")
                kind, message = client.read()
                self.assertEqual((kind, message["ok"], message["reason"]),
                                 (MessageType.REPLY_RESULT, False, RelayRefusal.NO_SUCH_REQUEST.value))
        self.assertEqual(self.uplink.of(LinkType.REPLY), [])
        client.send(MessageType.REPLY, request_id=rids[1], text="ok words", spoken="s")
        self.assertEqual(self.uplink.wait_for(LinkType.REPLY)["request_id"], rids[1])

    def test_reply_refused_locally_when_link_queue_full(self):
        self.start_relay()
        client = self.welcomed()
        rid = self.event(1)
        client.read()
        self.uplink.full = True
        client.send(MessageType.REPLY, request_id=rid, text="t", spoken="s")
        self.assertEqual(client.read()[1]["reason"], "desk link busy; the reply was not delivered")
        self.assertFalse(self.uplink.retired)

    def test_reply_recheck_failure_refuses_session_changed_then_unregisters(self):
        self.start_relay()
        client = self.welcomed()
        rid = self.event(1)
        client.read()
        self.mac.publish(sessionId=SESSION_B)  # /clear moved the parent to another session
        client.send(MessageType.REPLY, request_id=rid, text="t", spoken="s")
        self.assertEqual(client.read()[1]["reason"], "session changed")
        self.assertEqual(self.unregistered()["reason"], "relay_recheck_failed")
        self.assertEqual(self.uplink.of(LinkType.REPLY), [])

    def test_clear_between_register_and_event_undeliverable_before_any_write(self):
        self.start_relay()
        client = self.welcomed()
        self.mac.publish(sessionId=SESSION_B)
        rid = self.event(1)
        self.assertEqual(self.unregistered()["reason"], "relay_recheck_failed")
        self.assertEqual(self.undeliverable_ids(), {rid: "relay_recheck_failed"})
        self.assert_reports_before_unregistered()
        self.assertEqual(client.read(), (None, {}))  # nothing but the close reached the channel

    def test_event_for_closed_chan_answered_channel_closed(self):
        self.start_relay()
        client = self.welcomed()
        client.close()
        self.assertEqual(self.unregistered(), {"chan": 1, "reason": "channel_closed"})
        rid = self.event(1)
        self.assertEqual(self.undeliverable_ids(), {rid: "channel_closed"})

    def test_late_registered_reply_result_unregistered_ignored(self):
        # The relay's rows of "Late and unsolicited messages": for a `chan`
        # the relay has closed, a `registered` gets `unregistered
        # {channel_closed}`, and `register_refused`, `drop` and
        # `reply_result` are ignored; none is a bad frame, so the link stays.
        self.start_relay()
        client = self.welcomed()
        rid = self.event(1)
        self.assertEqual(client.read()[0], MessageType.EVENT)
        client.send(MessageType.REPLY, request_id=rid, text="answer", spoken="said")
        self.uplink.wait_for(LinkType.REPLY)  # the reply now waits for its result
        client.close()
        self.assertEqual(self.unregistered(), {"chan": 1, "reason": "channel_closed"})
        wait_until(lambda: self.relay.registered_count() == 0)
        before = len(self.uplink.frames)
        self.uplink.send(LinkType.REPLY_RESULT, chan=1, request_id=rid, ok=True, reason=None)
        self.uplink.send(LinkType.REGISTER_REFUSED, chan=1, reason="not_enrolled")
        self.uplink.send(LinkType.DROP, chan=1, reason="unenrolled")
        self.assertEqual(len(self.uplink.frames), before)  # ignored: nothing answered
        self.uplink.send(LinkType.REGISTERED, chan=1, generation=9)
        self.assertEqual(self.uplink.frames[before:],
                         [(LinkType.UNREGISTERED, {"chan": 1, "reason": "channel_closed"})])
        self.assertEqual({kind: self.relay.late[kind] for kind in ("reply_result", "register_refused", "drop",
                                                                    "registered")},
                         {"reply_result": 1, "register_refused": 1, "drop": 1, "registered": 1})
        self.assertFalse(self.uplink.retired)
        again = self.welcomed()  # the link still carries new channels
        self.assertEqual(self.uplink.of(LinkType.REGISTER)[-1]["chan"], 2)
        again.send(MessageType.PING)
        self.assertEqual(again.read()[0], MessageType.PONG)

    def test_hub_drop_closes_channel_without_report(self):
        self.start_relay()
        client = self.welcomed()
        self.uplink.send(LinkType.DROP, chan=1, reason="unenrolled")
        self.assertEqual(client.read(), (None, {}))
        wait_until(lambda: self.relay.registered_count() == 0)
        self.assertEqual(self.uplink.of(LinkType.UNREGISTERED), [])

    def test_link_down_closes_channels_and_reports_nothing(self):
        self.start_relay()
        client = self.welcomed()
        self.relay.link_down(self.uplink)
        self.assertEqual(client.read(), (None, {}))
        self.assertEqual(self.client().first[1]["reason"], "desk link down")
        self.assertEqual(self.uplink.of(LinkType.UNREGISTERED), [])


class WriterOutcomeTest(RelayCase):
    def blocked_event(self, clock, budget=0):
        client = self.welcomed()
        target = self.targets[0]
        target.budget = budget
        rid = self.event(1, "x" * 2000)
        wait_until(lambda: target.attempts > 1)  # welcome went out; the event's frame is begun
        return client, target, rid

    def test_relay_channel_write_unfinished_sends_no_undeliverable(self):
        for budget in (0, 40):
            with self.subTest(bytes_accepted=budget):
                self.mac = MacHome(self)
                self.config = self.mac.config
                self.targets = []
                clock = FakeClock()
                self.start_relay(clock=clock)
                self.blocked_event(clock, budget)
                clock.advance(5)
                self.assertEqual(self.unregistered()["reason"], "channel_write_failed")
                self.assertEqual(self.uplink.of(LinkType.UNDELIVERABLE), [])

    def test_closed_after_begun_mark_before_first_write_is_unfinished(self):
        clock = FakeClock()
        self.start_relay(clock=clock)
        client = self.welcomed()
        target = self.targets[0]
        target.before_send = lambda: client.sock.shutdown(socket.SHUT_RDWR)
        target.budget = 0
        self.event(1)  # the peer closes on the writer's first send of it, after its begun mark
        self.assertIn(self.unregistered()["reason"], {"channel_write_failed", "channel_closed"})
        self.assertEqual(self.uplink.of(LinkType.UNDELIVERABLE), [])

    def test_relay_sends_undeliverable_only_for_never_begun_events(self):
        def idle(clock):
            clock.advance(3)
        def recheck(clock):
            self.mac.kernel.failure = OSError("gone")
            clock.advance(2)
        for name, close, code in [("idle", idle, "channel_idle"), ("recheck", recheck, "relay_recheck_failed")]:
            with self.subTest(close=name):
                self.mac = MacHome(self)
                self.config = self.mac.config
                self.targets = []
                clock = FakeClock()
                self.start_relay(clock=clock, idle_seconds=3, recheck_seconds=2, write_deadline_seconds=60)
                _, _, begun = self.blocked_event(clock)
                queued = self.event(2)
                close(clock)
                self.assertEqual(self.unregistered()["reason"], code)
                self.assertEqual(self.undeliverable_ids(), {queued: "channel_not_written"})
                self.assertNotIn(begun, self.undeliverable_ids())
                self.assert_reports_before_unregistered()

    def test_full_channel_queue_busy(self):
        clock = FakeClock()
        self.start_relay(clock=clock, write_deadline_seconds=60)
        _, _, begun = self.blocked_event(clock)
        queued = [self.event(n) for n in range(2, 9)]  # 8 frames open: the queue is full
        overflow = self.event(9)
        self.assertEqual(self.unregistered()["reason"], "channel_busy")
        expected = {rid: "channel_not_written" for rid in queued} | {overflow: "channel_busy"}
        self.assertEqual(self.undeliverable_ids(), expected)
        self.assertNotIn(begun, expected)
        self.assert_reports_before_unregistered()

    def test_channel_write_deadline_includes_queue(self):
        clock = FakeClock()
        self.start_relay(clock=clock)
        client = self.welcomed()
        target = self.targets[0]
        first = protocol.encode(MessageType.EVENT, request_id=request_id(1), content="a")
        target.budget = 0
        self.event(1, "a")
        wait_until(lambda: target.attempts > 1)
        clock.advance(1)
        self.event(2, "b")  # submitted at t+1: due at t+6, whenever the writer gets to it
        clock.advance(3)
        target.budget = len(first)  # the first frame completes at t+4; the second then blocks
        wait_until(lambda: target.attempts > 4 and target.budget == 0)
        clock.advance(1.9)
        time.sleep(0.3)
        self.assertEqual(self.uplink.of(LinkType.UNREGISTERED), [])
        clock.advance(0.2)  # t+6.1: past the second frame's deadline, not t+4 plus 5
        self.assertEqual(self.unregistered()["reason"], "channel_write_failed")
        self.assertEqual(self.uplink.of(LinkType.UNDELIVERABLE), [])
        self.assertEqual(client.read()[0], MessageType.EVENT)

    def test_retire_from_other_thread_shuts_down_writer_closes(self):
        clock = FakeClock()
        self.start_relay(clock=clock)
        self.blocked_event(clock)
        target = self.targets[0]
        closer = threading.Thread(target=self.uplink.send, args=(LinkType.DROP,), kwargs={"chan": 1,
                                                                                         "reason": "unenrolled"})
        closer.start()
        closer.join(GENEROUS)
        wait_until(lambda: target.closed_by is not None)
        self.assertEqual(target.closed_by, "relay-chan-1")
        self.assertEqual(target.log[0], "shutdown")
        self.assertEqual(target.log[-1], "close")


class WireCodesTest(RelayCase):
    def test_every_emitted_code_is_in_its_table(self):
        """Each relay path that emits a code, driven once; FakeUplink checks
        every frame the way the hub's decoder does."""
        emitted = set()

        def collect():
            emitted.update((k, f["reason"]) for k, f in self.uplink.frames if "reason" in f)

        paths = [self._closed_path, self._idle_path, self._write_failed_path, self._busy_path, self._recheck_path,
                 self._channel_reports_path]
        for path in paths:
            with self.subTest(path=path.__name__):
                self.mac = MacHome(self)
                self.config = self.mac.config
                self.targets = []
                path()
                collect()
        for kind, code in emitted:
            self.assertIn(code, LINK_CODES[kind])
        self.assertEqual({c for k, c in emitted if k is LinkType.UNREGISTERED}, set(UnregisteredCode))
        self.assertEqual({c for k, c in emitted if k is LinkType.UNDELIVERABLE}, set(UndeliverableCode))

    def _closed_path(self):
        self.start_relay()
        self.welcomed().close()
        self.unregistered()
        self.event(1)

    def _idle_path(self):
        clock = FakeClock()
        self.start_relay(clock=clock)
        self.welcomed()
        clock.advance(IDLE_TIMEOUT_SECONDS)
        self.unregistered()

    def _write_failed_path(self):
        clock = FakeClock()
        self.start_relay(clock=clock)
        self.welcomed()
        self.targets[0].budget = 0
        self.event(1)
        wait_until(lambda: self.targets[0].attempts > 1)
        self.event(2)
        clock.advance(5)
        self.unregistered()

    def _busy_path(self):
        clock = FakeClock()
        self.start_relay(clock=clock)
        self.welcomed()
        self.targets[0].budget = 0
        for n in range(1, 10):
            self.event(n)
        self.unregistered()

    def _recheck_path(self):
        self.start_relay()
        self.welcomed()
        self.mac.publish(sessionId=SESSION_B)
        self.event(1)
        self.unregistered()

    def _channel_reports_path(self):
        self.start_relay()
        client = self.welcomed()
        reasons = [*UndeliverableReason, "something else"]
        for n, reason in enumerate(reasons, 1):
            client.send(MessageType.UNDELIVERABLE, request_id=request_id(n), reason=reason)
        wait_until(lambda: len(self.uplink.of(LinkType.UNDELIVERABLE)) == len(reasons))

    def test_final_check_reasons_mapped_to_codes(self):
        """The real channel's own guard (its parent's session file) refuses
        after a simulated /clear the relay's recheck didn't see."""
        cases = [(lambda: self.mac.publish_guard(SESSION_B), "guard_session_changed"),
                 (self.mac_remove_guard, "guard_session_file")]
        for change, code in cases:
            with self.subTest(code=code):
                self.mac = MacHome(self)
                self.config = self.mac.config
                self.start_relay()
                self.start_hub_link()
                self.uplink.wait_for(LinkType.REGISTER)
                wait_until(lambda: self.relay.registered_count() == 1)
                change()
                rid = self.event(1)
                self.uplink.wait_for(LinkType.UNDELIVERABLE)
                self.assertEqual(self.undeliverable_ids(), {rid: code})

    def mac_remove_guard(self):
        self.mac.remove_guard()


class HeartbeatTest(RelayCase):
    def test_healthy_idle_channel_kept_by_relay_pings(self):
        self.start_relay(heartbeat_seconds=0.3, idle_seconds=3)
        self.start_hub_link(heartbeat_seconds=1000, idle_timeout_seconds=3)
        wait_until(lambda: self.relay.registered_count() == 1, GENEROUS)
        time.sleep(4.5)  # longer than either side's idle timeout; only the relay's pings flow
        self.assertEqual(len(self.uplink.of(LinkType.REGISTER)), 1)
        self.assertEqual(self.uplink.of(LinkType.UNREGISTERED), [])
        self.assertEqual(self.relay.registered_count(), 1)
        self.assertEqual((HEARTBEAT_SECONDS, IDLE_TIMEOUT_SECONDS), (15, 45))

    def test_channel_blocked_on_stdout_unregistered_within_45s(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class NeverDrains(io.RawIOBase):
            def write(self, data):
                release.wait()
                return len(data)

        self.start_relay(heartbeat_seconds=1000, idle_seconds=2)
        self.start_hub_link(writer=StdoutWriter(NeverDrains()), heartbeat_seconds=0.3, idle_timeout_seconds=1000)
        wait_until(lambda: self.relay.registered_count() == 1, GENEROUS)
        time.sleep(2.5)  # the channel's own pings keep it registered while it can write
        self.assertEqual(self.uplink.of(LinkType.UNREGISTERED), [])
        self.event(1)  # its serve thread blocks writing this to Claude's stdin and goes silent
        self.assertEqual(self.unregistered()["reason"], "channel_idle")
        self.assertEqual(IDLE_TIMEOUT_SECONDS, 45)  # the relay's default idle rule


class ChannelIdentityTest(RelayCase):
    def test_channel_has_no_listener_check_on_either_os(self):
        """The channel connects to whatever listens on its socket and says
        hello; only the socket's mode keeps other users out."""
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                self.mac = MacHome(self)
                self.config = self.mac.config
                listener = bind_private_socket(self.config.channel_socket)
                self.addCleanup(listener.close)
                listener.settimeout(GENEROUS)
                with mock.patch.object(sys, "platform", platform), mock.patch.object(channel.sys, "platform",
                                                                                     platform):
                    self.start_hub_link()
                    conn, _ = listener.accept()
                self.addCleanup(conn.close)
                conn.settimeout(GENEROUS)
                kind, message = protocol.decode(conn.makefile("rb").readline())
                self.assertEqual((kind, message["session_id"]), (MessageType.HELLO, SESSION_A))


class BoundsTest(RelayCase):
    def test_channel_line_cap_at_relay(self):
        self.start_relay()
        client = self.welcomed()
        line = protocol.encode(MessageType.PING, pad="")
        padded = line[:-3] + b"x" * (protocol.MAX_LINE_BYTES - len(line)) + line[-3:]
        self.assertEqual(len(padded), protocol.MAX_LINE_BYTES)
        client.send_raw(padded)
        self.assertEqual(client.read()[0], MessageType.PONG)
        try:
            client.send_raw(b"x" * (protocol.MAX_LINE_BYTES + 1))
        except (BrokenPipeError, ConnectionResetError):
            pass  # The relay may close once the cap is passed, before the rest is sent.
        self.assertEqual(self.unregistered()["reason"], "channel_closed")

    def test_channel_connections_capped_at_accept(self):
        self.start_relay()
        silent = []
        for _ in range(8):  # unverified: connected, no hello yet
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(self.config.channel_socket))
            self.addCleanup(sock.close)
            silent.append(sock)
        wait_until(lambda: self.relay._unverified == 8)
        self.assert_no_answer()
        for sock in silent:
            sock.close()
        wait_until(lambda: self.relay._unverified == 0, GENEROUS)
        for _ in range(16):
            self.welcomed()
        self.assert_no_answer()
        self.assertEqual(len(self.uplink.of(LinkType.REGISTER)), 16)


if __name__ == "__main__":
    unittest.main()
