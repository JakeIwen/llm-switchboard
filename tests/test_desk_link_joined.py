"""The Mac link end to end: the real hub, the real pump in place of ssh,
the real relay (its macOS kernel calls faked behind peer_mac's reader, so
these run on Linux too) and the real `channel.HubLink` on the relay's
socket (docs/voice-desk.md, "Mac link", Tests: Disconnect, Wire codes,
Deadlines; the register, send, reply and Test round trips)."""

from concurrent.futures import ThreadPoolExecutor
import contextlib
import os
import signal
import threading
import time
import unittest
from unittest import mock

from desk_fixtures import ChannelClient, Clock, wait_until
from link_peer import MAC, MACHINE
from relay_fixture import CHANNEL_PID, GENEROUS, MAC_SESSION, JoinedCase
from llm_switchboard.desk import channel, link_session
from llm_switchboard.desk.link_protocol import REGISTER_WAIT_SECONDS
from llm_switchboard.desk.link_writer import SocketTarget
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import DeskError
from llm_switchboard.desk.protocol import MessageType
from llm_switchboard.desk.refusals import Refusal
from llm_switchboard.desk.relay_channels import RelayRefusal
from llm_switchboard.desk.relay_link import LinkEnd


class JoinedTest(JoinedCase):
    def client(self, timeout: float = GENEROUS) -> ChannelClient:
        """A raw channel on the relay's socket (the kernel says it is the
        flagged claude's desk channel), for tests that must see a frame the
        real channel would only summarize, or must stay silent."""
        client = ChannelClient(self.mac.config.channel_socket, MAC_SESSION, timeout=timeout)
        self.addCleanup(client.close)
        return client

    def welcomed(self) -> ChannelClient:
        client = self.client()
        self.assertEqual(client.first[0], MessageType.WELCOME, client.first)
        return client

    def assert_refused(self, client: ChannelClient, reason: RelayRefusal) -> None:
        self.assertEqual((client.first[0], client.first[1]["reason"]), (MessageType.REFUSED, reason.value))

    def assert_clear_refused(self, reason: str) -> None:
        with self.assertRaises(DeskError) as refused:
            self.clear_mac()
        self.assertEqual(refused.exception.reason, reason)


class RoundTripTest(JoinedTest):
    def test_mac_channel_registers_through_relay(self):
        self.start_relay()
        self.desk_channel()
        target = self.target(MAC_SESSION)
        self.assertEqual((target["host"], target["label"], target["cwd"], target["enrolled"]),
                         (MAC, "desk work", "/Users/me/dev/project", True))
        live = self.hub.links.live_link(MAC)
        self.assertEqual((live.machine, live.fingerprint), (MACHINE, self.key("mac").signer.fingerprint))
        self.assertEqual(live.link_id, self.relay_links[-1].link_id)

    def test_send_and_reply_round_trip(self):
        self.start_relay()
        desk = self.desk_channel()
        sent = self.send_mac("is the build green?")
        self.assertEqual(sent["state"], "handed_off")
        event = self.stdin.wait_event()
        self.assertEqual(event["meta"], {"request_id": sent["request_id"]})
        self.assertTrue(event["content"].startswith("is the build green?\n"), event)
        [lock] = LockStore(self.config.lock_file).load()
        self.assertEqual((lock.host, lock.machine, lock.channel_pid, lock.channel_start),
                         (MAC, MACHINE, CHANNEL_PID, str(self.mac.child.stamp.micros)))
        self.assertIsNone(desk.send_reply(sent["request_id"], "Green.", "It is green."))
        view = self.request_view(sent["request_id"])
        self.assertEqual((view["host"], view["state"], view["reply"]),
                         (MAC, "answered", {"text": "Green.", "spoken": "It is green."}))
        self.assertEqual(LockStore(self.config.lock_file).load(), [])

    def test_test_round_trip_marks_registration_replied(self):
        """The page's Test: a ticket and a send over HTTPS, answered through
        `reply` on the Mac registration, which then counts as replied."""
        self.start_relay()
        desk = self.desk_channel()
        target = self.target(MAC_SESSION)
        status, _, ticket = self.call("POST", "/desk/api/ticket", {
            "session_id": MAC_SESSION, "generation": target["generation"], "epoch": self.hub.epoch, "host": MAC},
            token=self.token)
        self.assertEqual(status, 200, ticket)
        status, _, sent = self.call("POST", "/desk/api/send", {"ticket": ticket["ticket"], "text": "Test"},
                                    token=self.token)
        self.assertEqual((status, sent["state"]), (200, "handed_off"), sent)
        self.assertEqual(self.stdin.wait_event()["meta"]["request_id"], sent["request_id"])
        self.assertEqual(self.target(MAC_SESSION)["registration"], "registered")
        self.assertIsNone(desk.send_reply(sent["request_id"], "Test received.", "Test received."))
        self.assertEqual(self.target(MAC_SESSION)["registration"], "replied")


class WireCodesTest(JoinedTest):
    def test_successful_reply_null_reason(self):
        """The hub's real `_handle_reply` answer for an accepted reply reaches
        the channel as `reply_result {ok: true, reason: null}`."""
        self.start_relay()
        client = self.welcomed()
        sent = self.send_mac()
        kind, event = client.read()
        self.assertEqual((kind, event["request_id"]), (MessageType.EVENT, sent["request_id"]))
        client.send(MessageType.DELIVERED, request_id=sent["request_id"])
        client.send(MessageType.REPLY, request_id=sent["request_id"], text="Done.", spoken="Done.")
        kind, result = client.read()
        self.assertEqual(kind, MessageType.REPLY_RESULT)
        self.assertEqual((result["request_id"], result["ok"]), (sent["request_id"], True))
        self.assertIn("reason", result)
        self.assertIsNone(result["reason"])
        self.assertEqual(self.hub.state.request_state(sent["request_id"]), "answered")


class DisconnectTest(JoinedTest):
    def test_channel_disconnect_unregistered_unknown_presence_gone_clears(self):
        self.start_relay()
        client = self.welcomed()
        sent = self.send_mac()
        self.assertEqual(client.read()[0], MessageType.EVENT)
        client.close()  # the channel leaves after the event reached it, before any report
        wait_until(lambda: self.target(MAC_SESSION)["registration"] == "none", GENEROUS)
        self.assertEqual(self.hub.state.request_state(sent["request_id"]), "unknown")
        self.assertEqual(self.lock_mac()["request_id"], sent["request_id"])
        self.assertEqual(self.links()[MAC]["state"], "up")
        # The relay answers the hub's presence query from the Mac's process table.
        self.assert_clear_refused(Refusal.CHANNEL_STILL_RUNNING)
        del self.mac.kernel.records[CHANNEL_PID]
        self.assertEqual(self.clear_mac(), {"cleared": True})
        self.assertIsNone(self.lock_mac())
        self.assertEqual(LockStore(self.config.lock_file).load(), [])

    def test_link_lost_with_relay_side_asserted(self):
        self.start_relay()
        client = self.welcomed()
        first_link = self.relay_links[-1]
        sent = self.send_mac()
        self.assertEqual(client.read()[0], MessageType.EVENT)
        self.backoff.release.clear()  # hold the relay's next attempt
        self.hub.links.retire(self.hub.links.live_link(MAC), "test")
        # The relay's side: the attempt ended as a closed link, every channel
        # was closed with nothing reported, and a new hello is refused.
        end = self.backoff.wait_end()
        self.assertEqual(end.code, LinkEnd.LINK_CLOSED)
        self.assertIsNotNone(end.up_seconds)
        self.assertTrue(first_link.retired)
        self.assertEqual(client.read(), (None, {}))
        self.assertEqual(self.relay.registered_count(), 0)
        self.assert_refused(self.client(), RelayRefusal.LINK_DOWN)
        # The hub's side: the written event is unknown and its lock stays.
        self.assertEqual(self.links()[MAC]["state"], "down")
        self.assertEqual(self.hub.state.request_state(sent["request_id"]), "unknown")
        self.assertEqual(self.lock_mac()["request_id"], sent["request_id"])
        self.assert_clear_refused(Refusal.MAC_NOT_CONNECTED)
        # The relay comes back on its own; the lock clears only by presence.
        self.backoff.release.set()
        self.wait_link("up")
        self.assertNotEqual(self.relay_links[-1].link_id, first_link.link_id)
        self.welcomed()
        self.assert_clear_refused(Refusal.CHANNEL_STILL_RUNNING)
        del self.mac.kernel.records[CHANNEL_PID]
        self.assertEqual(self.clear_mac(), {"cleared": True})


class _HeldPong:
    """The hub's link socket, with one `pong` held inside the kernel's send
    once armed: every byte accepted, the writer not yet told."""

    prefix = b'{"type":"pong"'

    def __init__(self):
        self.armed, self.held, self.release = threading.Event(), threading.Event(), threading.Event()
        hold = self

        class Target(SocketTarget):
            def send(self, view):
                if not (hold.armed.is_set() and bytes(view[:len(hold.prefix)]) == hold.prefix):
                    return super().send(view)
                hold.armed.clear()
                sent = super().send(view)
                hold.held.set()
                hold.release.wait(GENEROUS)
                return sent
        self.target = Target


class EventQueueWaitTest(JoinedTest):
    def setUp(self):
        # The hub's link clock is the test's, so a frame's 5 s pass on cue.
        self.link_clock = Clock()
        self.link_options = {"clock": self.link_clock, "tick_seconds": 3600}
        super().setUp()

    def test_event_wait_includes_queue_wait(self):
        """A Mac send's event frame is due 5 s after it was queued, not
        after the frames ahead of it are written: queued behind a pong the
        writer is still finishing, it is never begun once its 5 s have
        passed, the link retires, and the lock clears with no presence
        check while the channel still runs."""
        hold = _HeldPong()
        patcher = mock.patch.object(link_session, "SocketTarget", hold.target)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(hold.release.set)
        self.start_relay(heartbeat_seconds=0.2)  # the relay pings often, so the hub soon has a pong to write
        self.desk_channel()
        writer = self.hub.links.live_link(MAC).writer
        queued = threading.Event()
        submit = writer.submit

        def noting_events(data, *args, **kwargs):
            frame = submit(data, *args, **kwargs)
            if data.startswith(b'{"type":"event"'):
                queued.set()
            return frame
        writer.submit = noting_events
        self.backoff.release.clear()
        hold.armed.set()
        self.assertTrue(hold.held.wait(GENEROUS))
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.send_mac)
            self.assertTrue(queued.wait(GENEROUS))
            self.link_clock.advance(5.1)
            hold.release.set()
            sent = pending.result(GENEROUS)
        self.assertEqual(sent["state"], "unknown")
        self.wait_link("down")
        self.assertEqual(self.stdin.lines, [])
        self.assertIn(CHANNEL_PID, self.mac.kernel.records)
        self.assertEqual(self.clear_mac(), {"cleared": True})


class RegisterWaitTest(JoinedTest):
    def test_register_wait_3_5s_then_refused(self):
        self.start_relay()
        pump = self.relay_pump()
        os.kill(pump, signal.SIGSTOP)  # the link stalls: the register reaches no hub for now
        self.addCleanup(self._resume, pump)
        started = time.monotonic()
        refused = self.client()
        waited = time.monotonic() - started
        self.assert_refused(refused, RelayRefusal.LINK_SLOW)
        self.assertGreaterEqual(waited, REGISTER_WAIT_SECONDS)
        self.assertEqual(REGISTER_WAIT_SECONDS, 3.5)
        self.assertLess(REGISTER_WAIT_SECONDS, channel.WELCOME_WAIT_SECONDS)
        self._resume(pump)
        # The hub registers it late; the relay answers that with `unregistered`.
        wait_until(lambda: self.relay.late["registered"] == 1, GENEROUS)
        wait_until(lambda: self.target(MAC_SESSION)["registration"] == "none", GENEROUS)
        again = self.welcomed()
        self.assertEqual(again.first[1]["generation"], 2)
        self.assertEqual(self.target(MAC_SESSION)["generation"], 2)

    @staticmethod
    def _resume(pid: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)


if __name__ == "__main__":
    unittest.main()
