"""Mac channels over the link on the hub (docs/voice-desk.md, "Hub side",
"Writers: one outcome per frame", "Targets, tickets, requests, locks and
replies"): remote registrations, sends and their three outcomes, replies,
presence at clear and the link-wait caps, with a real hub driven by the
test-side relay in tests/link_peer.py."""

import socket
import threading
import unittest

from desk_fixtures import SESSION_A, HubCase, wait_until
from link_peer import MAC, MAC2, MACHINE, OTHER_MACHINE, LinkHubCase, write_signers

from llm_switchboard.desk.link_hub import LinkWaitSurface
from llm_switchboard.desk.link_protocol import LinkType
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import DeskError, WriteOutcome
from llm_switchboard.desk.protocol import MessageType
from llm_switchboard.desk.refusals import Refusal

SESSION_M = "dddddddd-0000-4000-8000-000000000004"


class MacTargetCase(LinkHubCase, HubCase):
    def setUp(self):
        super().setUp()
        self.remote[SESSION_M] = MAC
        self.hub.refresh_files()
        self.mac = self.connect()
        self.assertEqual(self.mac.register(1, SESSION_M, channel_pid=4321, channel_start="777", label="build",
                                           cwd="/Users/me/x")["type"], "registered")

    def send_m(self, text: str = "how is the build?") -> dict:
        target = self.target(SESSION_M)
        ticket = self.hub.ticket({"session_id": SESSION_M, "generation": target["generation"],
                                  "epoch": self.hub.epoch}, self.token)["ticket"]
        return self.hub.send({"ticket": ticket, "text": text}, self.token)

    def lock_m(self) -> dict:
        return self.target(SESSION_M)["lock"]

    def clear_m(self, *, operator: bool = False):
        lock = self.lock_m()
        body = {"session_id": SESSION_M, "lock_id": lock["lock_id"]}
        if operator:
            return self.hub.handle_operator({"op": "clear", **body})
        return self.hub.clear(body, self.token)

    def clear_in_thread(self) -> dict:
        result = {}

        def run():
            try:
                result["value"] = self.clear_m()
            except DeskError as exc:
                result["error"] = exc
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        result["thread"] = thread
        return result

    def unknown_after_disconnect(self) -> str:
        """A written event whose channel then left: unknown, lock kept."""
        sent = self.send_m()
        self.mac.expect(LinkType.EVENT)
        self.mac.send(LinkType.UNREGISTERED, chan=1, reason="channel_closed")
        wait_until(lambda: self.hub.state.request_state(sent["request_id"]) == "unknown")
        return sent["request_id"]


class MacSendTest(MacTargetCase):
    def test_round_trip_lock_fields_and_null_reason(self):
        self.assertEqual((self.target(SESSION_M)["host"], self.target(SESSION_M)["label"]), (MAC, "build"))
        sent = self.send_m()
        self.assertEqual(sent["state"], "handed_off")
        event = self.mac.expect(LinkType.EVENT)
        self.assertEqual((event["chan"], event["request_id"]), (1, sent["request_id"]))
        [lock] = LockStore(self.config.lock_file).load()
        self.assertEqual((lock.host, lock.link_key, lock.machine, lock.channel_pid, lock.channel_start),
                         (MAC, self.key("mac").signer.fingerprint, MACHINE, 4321, "777"))
        self.mac.send(LinkType.DELIVERED, chan=1, request_id=sent["request_id"])
        self.mac.send(LinkType.REPLY, chan=1, request_id=sent["request_id"], text="Green.", spoken="It is green.")
        result = self.mac.expect(LinkType.REPLY_RESULT)
        self.assertEqual(result, {"chan": 1, "request_id": sent["request_id"], "ok": True, "reason": None})
        view = next(r for r in self.hub.state_view()["requests"] if r["request_id"] == sent["request_id"])
        self.assertEqual((view["host"], view["state"]), (MAC, "answered"))
        self.assertEqual(LockStore(self.config.lock_file).load(), [])

    def test_reply_refusal_carried_as_code(self):
        sent = self.send_m()
        self.mac.expect(LinkType.EVENT)
        self.mac.send(LinkType.REPLY, chan=1, request_id=f"{self.hub.epoch}.{'0' * 16}", text="x", spoken="")
        self.assertEqual(self.mac.expect(LinkType.REPLY_RESULT)["reason"], "unknown_request")
        self.assertEqual(self.hub.state.request_state(sent["request_id"]), "handed_off")

    def _send_with(self, after_handoff) -> dict:
        original = self.hub._prepare_handoff

        def prepare(request):
            prepared = original(request)
            after_handoff(self.hub.links.live_link(MAC))
            return prepared
        self.hub._prepare_handoff = prepare
        try:
            return self.send_m()
        finally:
            self.hub._prepare_handoff = original

    def test_hub_send_three_outcomes(self):
        # not_begun: the link retired between the handoff and the write.
        sent = self._send_with(lambda link: self.hub.links.retire(link, "test"))
        self.assertEqual(sent["state"], "unknown")
        self.assertIsNotNone(self.lock_m())
        self.assertEqual(self.clear_m(), {"cleared": True})  # nothing could be written
        # unfinished: the write fails after the begun mark.
        self.mac = self.connect()
        self.mac.register(1, SESSION_M)
        sent = self._send_with(lambda link: link.sock.shutdown(socket.SHUT_WR))
        self.assertEqual(sent["state"], "unknown")
        # The writer decides the frame before it reports the failed link.
        wait_until(lambda: self.hub.links.live_link(MAC) is None)
        with self.assertRaises(DeskError) as raised:
            self.clear_m()  # a presence check, and that link is gone
        self.assertEqual(raised.exception.reason, Refusal.MAC_NOT_CONNECTED)

    def test_retired_proxy_write_fails_without_lookup(self):
        [registration] = self.hub.state.registrations()
        proxy = self.hub._connections[registration.conn_id]
        old = proxy.link
        successor = self.connect()
        self.assertTrue(old.retired)
        self.assertEqual(successor.register(9, SESSION_M)["type"], "registered")
        self.assertIs(proxy.send_event(f"{self.hub.epoch}.{'0' * 16}", "x"), WriteOutcome.NOT_BEGUN)
        self.assertEqual(len(self.hub.state.registrations()), 1)

    def test_recheck_drops_unenrolled_mac_registration(self):
        del self.remote[SESSION_M]
        self.hub.recheck_now()
        self.assertEqual(self.mac.expect(LinkType.DROP), {"chan": 1, "reason": "hub_recheck_failed"})
        self.assertEqual(self.hub.state.registrations(), [])
        self.assertEqual(self.links()[MAC]["state"], "up")


class MacPresenceTest(MacTargetCase):
    def test_presence_clear_running_then_gone(self):
        self.unknown_after_disconnect()
        for state, expect in (("running", Refusal.CHANNEL_STILL_RUNNING), ("gone", None)):
            pending = self.clear_in_thread()
            query = self.mac.expect(LinkType.PRESENCE_QUERY)
            self.assertEqual((query["channel_pid"], query["channel_start"]), (4321, "777"))
            self.mac.send(LinkType.PRESENCE, query_id=query["query_id"], state=state)
            pending["thread"].join(10)
            if expect:
                self.assertEqual(pending["error"].reason, expect)
            else:
                self.assertEqual(pending["value"], {"cleared": True})

    def test_presence_from_other_machine_is_unverifiable(self):
        self.unknown_after_disconnect()
        self.mac = self.connect(machine=OTHER_MACHINE)
        with self.assertRaises(DeskError) as raised:
            self.clear_m()
        self.assertEqual(raised.exception.reason, Refusal.MAC_NOT_CONNECTED)

    def test_presence_from_link_with_other_key_is_unverifiable(self):
        self.unknown_after_disconnect()
        write_signers(self.config.link_signers_file, self.key("mac-rotated").signer, self.key("mac2").signer)
        self.hub.recheck_now()
        self.mac = self.connect("mac-rotated")
        with self.assertRaises(DeskError) as raised:
            self.clear_m()
        self.assertEqual(raised.exception.reason, Refusal.MAC_NOT_CONNECTED)

    def test_late_presence_after_timeout_ignored(self):
        self.unknown_after_disconnect()
        self.hub.links.presence_seconds = 0.3
        pending = self.clear_in_thread()
        query = self.mac.expect(LinkType.PRESENCE_QUERY)
        pending["thread"].join(10)
        self.assertEqual(pending["error"].reason, Refusal.CHANNEL_UNVERIFIABLE)
        self.mac.send(LinkType.PRESENCE, query_id=query["query_id"], state="gone")
        self.mac.send(LinkType.PING, n=1)
        self.assertEqual(self.mac.expect(LinkType.PONG), {"n": 1})
        self.assertIsNotNone(self.lock_m())

    def test_presence_answer_bound_to_query_and_link(self):
        self.unknown_after_disconnect()
        other = self.connect("mac2", MAC2)
        pending = self.clear_in_thread()
        query = self.mac.expect(LinkType.PRESENCE_QUERY)
        other.send(LinkType.PRESENCE, query_id=query["query_id"], state="gone")
        self.assertTrue(other.closed())  # that link issued no query
        self.assertTrue(pending["thread"].is_alive())
        self.mac.send(LinkType.PRESENCE, query_id=query["query_id"], state="gone")
        pending["thread"].join(10)
        self.assertEqual(pending["value"], {"cleared": True})

    def test_reported_delivery_clears_without_presence(self):
        sent = self.send_m()
        self.mac.expect(LinkType.EVENT)
        self.mac.send(LinkType.UNDELIVERABLE, chan=1, request_id=sent["request_id"], reason="guard_session_file")
        wait_until(lambda: self.hub.state.request_state(sent["request_id"]) == "unknown")
        self.assertEqual(self.clear_m(), {"cleared": True})

    def test_link_lost_after_written_needs_presence(self):
        sent = self.send_m()
        self.mac.expect(LinkType.EVENT)
        self.mac.close()
        wait_until(lambda: self.hub.state.request_state(sent["request_id"]) == "unknown")
        self.assertIsNotNone(self.lock_m())
        with self.assertRaises(DeskError) as raised:
            self.clear_m()
        self.assertEqual(raised.exception.reason, Refusal.MAC_NOT_CONNECTED)


class LinkWaitCapTest(MacTargetCase):
    def test_busy_send_expires_request_before_it_writes_lock(self):
        held = [self.hub.links.wait_slot(LinkWaitSurface.HTTP) for _ in range(4)]
        with self.assertRaises(DeskError) as raised:
            self.send_m()
        self.assertEqual(raised.exception.reason, Refusal.LINK_BUSY)
        self.assertIsNone(self.lock_m())
        self.assertEqual([r["state"] for r in self.hub.state_view()["requests"]], ["expired"])
        # A Pi send needs no link slot.
        channel = self.channel(SESSION_A)
        target = self.target(SESSION_A)
        ticket = self.hub.ticket({"session_id": SESSION_A, "generation": target["generation"],
                                  "epoch": self.hub.epoch}, self.token)["ticket"]
        self.assertEqual(self.hub.send({"ticket": ticket, "text": "pi"}, self.token)["state"], "handed_off")
        self.assertIs(channel.read()[0], MessageType.EVENT)
        for slot in held:
            slot.__exit__(None, None, None)
        self.unknown_after_disconnect()
        operator = self.hub.links.wait_slot(LinkWaitSurface.OPERATOR)
        answer = self.clear_m(operator=True)
        self.assertEqual(answer, {"ok": False, "error": Refusal.LINK_OPERATOR_BUSY})
        operator.__exit__(None, None, None)
        self.assertEqual(self.hub.handle_operator({"op": "status"})["ok"], True)


if __name__ == "__main__":
    unittest.main()
