"""The relay's side of the Mac link (relay_link, docs/voice-desk.md,
"Admission and proof of possession", "Deadlines, heartbeats and
backpressure", "Link freshness and sleep"): the handshake, mixed versions,
dispatch, heartbeats and the freshness rule. The real pump (`bin/desk-link`)
stands in for ssh between a `RelayLink` and a test-side hub (`FakeHub`) or
a real hub; a fake CLOCK_MONOTONIC drives the link's timing rules."""

import threading
import unittest
from unittest import mock

from desk_fixtures import SESSION_A, ChannelClient, Clock, HubCase, request_id, wait_until
from link_peer import MACHINE, LinkHubCase
from relay_fixture import (
    CHANNEL_PID, EPOCH, FAKE_SIGNATURE, GENEROUS, LINK_ID, NONCE, FakeChannels, GatedSocket, LinkCase, Run,
    pump_invocation,
)
from llm_switchboard.desk import link_admission
from llm_switchboard.desk.link_proof import SigningFailed, SigningFailure, proof_message
from llm_switchboard.desk.link_protocol import (
    FIXED_BACKOFF_SECONDS, LINK_QUEUE_FRAMES, LinkFrameError, LinkType, PresenceState, RefusedCode, Side,
    encode_frame,
)
from llm_switchboard.desk.protocol import PROTOCOL_VERSION, MessageType
from llm_switchboard.desk.relay_channels import RelayChannels, RelayRefusal
from llm_switchboard.desk.relay_link import Backoff, LinkEnd, RelayLink, fixed_backoff


class HandshakeTest(LinkCase):
    def test_hello_proof_and_welcome(self):
        signed = []

        def sign(message, deadline_seconds):
            signed.append((message, deadline_seconds))
            return FAKE_SIGNATURE

        run = self.start(sign=sign, relay_build="abc1234")
        self.hub.accept()
        hello = self.hub.expect(LinkType.LINK_HELLO)
        self.assertEqual(hello, {"versions": [1], "relay_build": "abc1234", "host": "mac", "machine": MACHINE})
        self.hub.send(LinkType.LINK_CHALLENGE, version=1, epoch=EPOCH, nonce=NONCE)
        self.hub.version = 1
        self.assertEqual(self.hub.expect(LinkType.LINK_PROOF), {"signature": FAKE_SIGNATURE})
        self.assertEqual(signed, [(proof_message(version=1, host="mac", machine=MACHINE, epoch=EPOCH, nonce=NONCE), 5)])
        self.assertEqual(self.channels.calls, [])  # nothing reaches the channels before the welcome
        self.hub.welcome()
        self.assertEqual(self.channels.wait_for("up"), [("up", run.link)])
        self.assertEqual((run.link.epoch, run.link.link_id, run.link.version), (EPOCH, LINK_ID, 1))

    def test_retired_link_closes_channels_and_ssh(self):
        run = self.up()
        run.link.retire("test")
        self.assert_down(run, LinkEnd.RETIRED)
        self.assertTrue(self.hub.closed())
        self.assertTrue(run.link.child.reaped.wait(GENEROUS))
        self.assertFalse(run.link.submit(LinkType.PING, n=1))

    def test_hub_eof_ends_link(self):
        run = self.up()
        self.hub.close()
        attempt = self.assert_down(run, LinkEnd.LINK_CLOSED)
        self.assertGreaterEqual(attempt.up_seconds, 0)

    def test_refusals_end_the_attempt_with_their_code(self):
        for code in RefusedCode:
            if code is RefusedCode.VERSION:
                continue
            with self.subTest(code=code.value):
                self.channels = FakeChannels()
                run = self.start()
                self.hub.accept()
                self.hub.expect(LinkType.LINK_HELLO)
                self.hub.send(LinkType.LINK_REFUSED, reason=code.value, versions=[1])
                attempt = self.assert_down(run, code)
                self.assertIsNone(attempt.up_seconds)
                self.assertNotIn("up", [call[0] for call in self.channels.calls])

    def test_refusal_after_proof(self):
        run = self.start()
        self.hub.challenge()
        self.hub.send(LinkType.LINK_REFUSED, reason=RefusedCode.PROOF_FAILED.value, versions=[1])
        self.assertTrue(fixed_backoff(self.assert_down(run, RefusedCode.PROOF_FAILED)))

    def test_no_hub_is_hub_not_running(self):
        self.hub.close()
        self.mac.config.link_socket.unlink()
        run = self.start()
        self.assert_down(run, RefusedCode.HUB_NOT_RUNNING)

    def test_signing_failure_ends_attempt(self):
        def sign(message, deadline_seconds):
            raise SigningFailed(SigningFailure.SIGNER_UNAVAILABLE)

        run = self.start(sign=sign)
        self.hub.accept()
        self.hub.expect(LinkType.LINK_HELLO)
        with self.assertLogs("llm_switchboard.desk.relay", "WARNING") as logs:
            self.hub.send(LinkType.LINK_CHALLENGE, version=1, epoch=EPOCH, nonce=NONCE)
            self.assert_down(run, LinkEnd.SIGN_FAILED)
        self.assertIn("signer_unavailable", logs.output[0])


class MixedVersionsTest(LinkCase):
    def test_relay_v1_against_hub_v2_refused(self):
        run = self.start()
        self.hub.accept()
        self.assertEqual(self.hub.expect(LinkType.LINK_HELLO)["versions"], [1])
        self.hub.send(LinkType.LINK_REFUSED, reason="version", versions=[2])
        attempt = self.assert_down(run, RefusedCode.VERSION, RelayRefusal.LINK_VERSION)
        self.assertEqual(attempt.hub_versions, (2,))
        self.assertEqual(Backoff().after(attempt), FIXED_BACKOFF_SECONDS)

    def test_relay_v2_against_hub_v1_refused(self):
        run = self.start(versions=(2,))
        self.hub.accept()
        self.assertEqual(self.hub.expect(LinkType.LINK_HELLO)["versions"], [2])
        self.hub.send(LinkType.LINK_REFUSED, reason="version", versions=[1])
        attempt = self.assert_down(run, RefusedCode.VERSION, RelayRefusal.LINK_VERSION)
        self.assertEqual(attempt.hub_versions, (1,))

    def test_challenge_for_a_version_not_offered_closes(self):
        run = self.start()
        self.hub.accept()
        self.hub.expect(LinkType.LINK_HELLO)
        self.hub.send(LinkType.LINK_CHALLENGE, version=2, epoch=EPOCH, nonce=NONCE)
        self.assert_down(run, LinkEnd.BAD_FRAME)

    def test_frame_with_another_v_after_challenge_closes(self):
        run = self.start()
        self.hub.challenge()
        self.hub.send_raw(encode_frame(LinkType.LINK_WELCOME, Side.HUB, v=2, link_id=LINK_ID, heartbeat_seconds=15,
                                       idle_timeout_seconds=45, max_line_bytes=80 * 1024))
        self.assert_down(run, LinkEnd.BAD_FRAME)
        self.assertNotIn("up", [call[0] for call in self.channels.calls])

    def test_frame_with_another_v_on_the_link_closes(self):
        run = self.up()
        self.hub.send_raw(encode_frame(LinkType.EVENT, Side.HUB, v=2, chan=1, request_id=request_id(1), content="x"))
        self.assert_down(run, LinkEnd.BAD_FRAME)
        self.assertEqual(self.channels.frames(), [])

    def test_unknown_type_closes(self):
        run = self.up()
        self.hub.send_raw(b'{"type":"link_bogus","v":1}\n')
        self.assert_down(run, LinkEnd.BAD_FRAME)

    def test_handshake_frame_on_the_link_closes(self):
        run = self.up()
        self.hub.send(LinkType.LINK_REFUSED, reason="version", versions=[1])
        self.assert_down(run, LinkEnd.BAD_FRAME)

    def test_first_line_with_a_v_is_no_link_frame(self):
        # The version-free shapes never carry `v`: a first line that does
        # isn't the forced command speaking the link protocol.
        run = self.start()
        self.hub.accept()
        self.hub.expect(LinkType.LINK_HELLO)
        self.hub.send_raw(b'{"type":"link_challenge","v":1,"version":1,"epoch":"' + EPOCH.encode()
                          + b'","nonce":"' + NONCE.encode() + b'"}\n')
        attempt = self.assert_down(run, LinkEnd.FORCED_COMMAND_FAILED)
        self.assertTrue(fixed_backoff(attempt))


class DispatchTest(LinkCase):
    def test_channel_frames_reach_channels_in_order(self):
        run = self.up()
        self.event(1)
        self.hub.send(LinkType.DROP, chan=1, reason="unenrolled")
        self.hub.send(LinkType.REGISTERED, chan=2, generation=3)
        self.round_trip(1)
        self.assertEqual([kind for kind, _ in self.channels.frames()],
                         [LinkType.EVENT, LinkType.DROP, LinkType.REGISTERED])
        self.assertFalse(run.link.retired)

    def test_channel_violation_closes_link(self):
        run = self.up()
        self.channels.violation = True
        self.event(1)
        self.assert_down(run, LinkEnd.BAD_FRAME)

    def test_presence_answered_from_the_kernel(self):
        self.up()
        start = str(self.mac.child.stamp.micros)
        cases = [(CHANNEL_PID, start, PresenceState.RUNNING), (CHANNEL_PID, str(int(start) + 1), PresenceState.GONE),
                 (999, start, PresenceState.GONE)]
        for query_id, (pid, channel_start, state) in enumerate(cases, 1):
            self.hub.send(LinkType.PRESENCE_QUERY, query_id=query_id, channel_pid=pid, channel_start=channel_start)
            self.assertEqual(self.read_until(LinkType.PRESENCE)[0], {"query_id": query_id, "state": state.value})
        self.mac.kernel.failure = PermissionError()
        self.hub.send(LinkType.PRESENCE_QUERY, query_id=4, channel_pid=CHANNEL_PID, channel_start=start)
        self.assertEqual(self.read_until(LinkType.PRESENCE)[0], {"query_id": 4, "state": "unverifiable"})

    def test_submit_refusals(self):
        run = self.up(awake_clock=Clock())
        with self.assertRaises(LinkFrameError):
            run.link.submit(LinkType.UNREGISTERED, chan=1, reason="free text")
        self.targets[0].full = True
        results = [run.link.submit(LinkType.DELIVERED, chan=1, request_id=request_id(n))
                   for n in range(LINK_QUEUE_FRAMES + 1)]
        self.assertEqual(results, [True] * LINK_QUEUE_FRAMES + [False])
        # The relay's own ping is skipped, with no record. The second event
        # is read after the tick that follows the first, so the ping is due by then.
        self.clock.advance(15)
        self.event(1)
        self.event(2)
        self.channels.wait_for("frame", 2)
        self.assertEqual(run.link.ping_records(), {})
        # A presence answer can't be queued: the link retires.
        self.hub.send(LinkType.PRESENCE_QUERY, query_id=1, channel_pid=CHANNEL_PID, channel_start="1")
        self.assert_down(run, LinkEnd.QUEUE_FULL)
        self.assertFalse(run.link.submit(LinkType.DELIVERED, chan=1, request_id=request_id(1)))


class HeartbeatTest(LinkCase):
    def test_hub_ping_answered(self):
        self.up()
        self.assertEqual(self.round_trip(7), [])

    def test_relay_pings_every_15s_under_traffic(self):
        run = self.up()
        start = self.clock()
        seen = []
        for step in range(1, 13):
            self.clock.advance(5)
            # The relay's ping comes from the tick after the first pong; the
            # second round trip catches it at this clock value.
            for kind, fields in self.round_trip(100 + 2 * step) + self.round_trip(101 + 2 * step):
                if kind is LinkType.PING:
                    seen.append((fields["n"], self.clock() - start))
                    self.hub.send(LinkType.PONG, n=fields["n"])
        for kind, fields in self.round_trip(200):
            if kind is LinkType.PING:
                seen.append((fields["n"], self.clock() - start))
        self.assertEqual(seen, [(1, 15), (2, 30), (3, 45), (4, 60)])
        self.assertFalse(run.link.retired)

    def test_idle_45s_without_a_hub_frame_retires(self):
        run = self.up()
        for _ in range(2):
            self.clock.advance(15)
            self.hub.send(LinkType.PONG, n=self.relay_ping())
        self.round_trip(1)
        self.clock.advance(44)
        self.relay_ping()  # still up at 44 s
        self.clock.advance(1)
        self.assert_down(run, LinkEnd.IDLE)

    def test_pong_for_unsent_ping_closes_link(self):
        run = self.up()
        self.hub.send(LinkType.PONG, n=1)
        self.assert_down(run, LinkEnd.BAD_FRAME)

    def test_stale_pong_changes_nothing(self):
        run = self.up(idle_seconds=1000)
        t0 = self.clock()
        self.clock.advance(15)
        self.hub.send(LinkType.PONG, n=self.relay_ping())
        wait_until(lambda: run.link.acked_at == t0 + 15)
        # Already matched: ignored.
        self.clock.advance(15)
        self.assertEqual(self.relay_ping(), 2)
        self.hub.send(LinkType.PONG, n=1)
        self.round_trip(1)
        self.assertEqual(run.link.acked_at, t0 + 15)
        # Expired (40 s old): dropped by then, ignored, no violation.
        self.clock.advance(41)
        self.assertEqual(self.relay_ping(), 3)
        self.assertNotIn(2, run.link.ping_records())
        self.hub.send(LinkType.PONG, n=2)
        self.hub.send(LinkType.PONG, n=3)
        wait_until(lambda: run.link.acked_at == t0 + 71)
        self.round_trip(2)
        self.assertFalse(run.link.retired)

    def test_unmatched_ping_records_bounded_and_expire(self):
        run = self.up(heartbeat_seconds=5, idle_seconds=1000)
        t0 = self.clock()
        for n in range(1, 7):
            self.clock.advance(5)
            self.assertEqual(self.relay_ping(), n)
        wait_until(lambda: run.link.ping_records() == {3: t0 + 15, 4: t0 + 20, 5: t0 + 25, 6: t0 + 30})
        self.clock.advance(26)
        self.assertEqual(self.relay_ping(), 7)
        self.clock.advance(10)
        self.assertEqual(self.relay_ping(), 8)
        wait_until(lambda: set(run.link.ping_records()) == {6, 7, 8})
        now = self.clock()
        self.assertTrue(all(now - t < 40 for t in run.link.ping_records().values()))


class FreshnessTest(LinkCase):
    def test_freshness_from_time_read_before_ping_enqueued(self):
        run = self.up()
        t0 = self.clock()
        self.clock.advance(15)
        self.assertEqual(self.relay_ping(), 1)
        self.clock.advance(24)
        self.assertEqual(self.relay_ping(), 2)
        self.hub.send(LinkType.PONG, n=1)
        # The time read before ping 1 was enqueued, not the pong's arrival.
        wait_until(lambda: run.link.acked_at == t0 + 15)
        self.clock.advance(16)
        self.event(1)
        self.assert_down(run, LinkEnd.STALE)
        self.assertEqual(self.channels.frames(), [])

    def test_frames_not_dispatched_40s_after_ack(self):
        # The idle rule out of the way, so the freshness rule alone decides.
        run = self.up(idle_seconds=1000)
        self.clock.advance(50)
        self.event(1)
        self.hub.send(LinkType.DROP, chan=1, reason="unenrolled")
        attempt = self.assert_down(run, LinkEnd.STALE)
        self.assertEqual(self.channels.frames(), [])
        self.assertIsNotNone(attempt.up_seconds)

    def test_frames_not_dispatched_after_jump_with_idle_rule(self):
        # With the 45 s idle rule too, whichever of the two the tick reaches
        # first ends the link; nothing is dispatched either way.
        run = self.up()
        self.clock.advance(50)
        self.event(1)
        self.hub.send(LinkType.DROP, chan=1, reason="unenrolled")
        attempt = run.ended()
        self.assertIn(attempt.code, (LinkEnd.IDLE, LinkEnd.STALE))
        self.assertEqual(self.channels.frames(), [])

    def test_short_jump_dispatches_in_order(self):
        run = self.up()
        self.clock.advance(10)
        self.event(1)
        self.hub.send(LinkType.DROP, chan=1, reason="unenrolled")
        self.event(2)
        self.round_trip(1)
        self.assertEqual([(kind, fields.get("request_id")) for kind, fields in self.channels.frames()],
                         [(LinkType.EVENT, request_id(1)), (LinkType.DROP, None), (LinkType.EVENT, request_id(2))])
        self.assertFalse(run.link.retired)

    def test_awake_scheduling_delay_does_not_retire(self):
        run = self.up()
        self.clock.advance(15)
        pending = self.relay_ping()
        for n in range(1, 7):
            # Each pong arrives 15 to 20 s after its ping was enqueued.
            self.clock.advance(20 if n == 1 else 15)
            following = self.relay_ping()
            self.hub.send(LinkType.PONG, n=pending)
            self.event(n)
            self.round_trip(n)
            pending = following
        self.assertEqual(len(self.channels.frames()), 6)
        self.assertFalse(run.link.retired)

    def test_suspension_between_kernel_accept_and_bookkeeping(self):
        # A ping: its bytes reach the hub, which answers, and the relay is
        # suspended 50 s before its writer does anything more.
        run = self.up(idle_seconds=1000)
        self.targets[0].hold()
        self.clock.advance(15)
        self.assertEqual(self.relay_ping(), 1)
        self.assertTrue(self.targets[0].held.wait(GENEROUS))
        self.clock.advance(50)
        self.hub.send(LinkType.PONG, n=1)
        self.event(1)
        self.targets[0].release()
        self.assert_down(run, LinkEnd.STALE)
        self.assertEqual(self.channels.frames(), [])

    def test_suspension_between_kernel_accept_and_bookkeeping_for_proof(self):
        # The same for link_proof and link_welcome.
        def held_after_hello(fd):
            target = self.gated(fd)
            target.hold(after=1)  # the hello passes; the proof is held
            return target

        run = self.start(idle_seconds=1000, handshake_seconds=1000, target_factory=held_after_hello)
        self.hub.challenge()
        self.assertTrue(self.targets[0].held.wait(GENEROUS))
        self.clock.advance(50)
        self.hub.welcome()
        self.event(1)
        self.targets[0].release()
        self.assert_down(run, LinkEnd.STALE)
        self.assertEqual(self.channels.frames(), [])

    def test_write_spanning_sleep_decided_by_writer(self):
        # A frame half accepted when the Mac sleeps: the writer finishes it
        # on awake time, and the jump on CLOCK_MONOTONIC expires nothing.
        run = self.up()
        self.targets[0].hold(keep=10)
        self.assertTrue(run.link.submit(LinkType.DELIVERED, chan=1, request_id=request_id(1)))
        self.assertTrue(self.targets[0].held.wait(GENEROUS))
        self.clock.advance(30)
        self.targets[0].release()
        self.assertEqual(self.read_until(LinkType.DELIVERED)[0], {"chan": 1, "request_id": request_id(1)})
        self.round_trip(1)
        self.assertFalse(run.link.retired)

    def test_ssh_writer_deadline_on_awake_time_retires(self):
        awake = Clock()
        run = self.up(awake_clock=awake)
        self.targets[0].full = True
        self.assertTrue(run.link.submit(LinkType.DELIVERED, chan=1, request_id=request_id(1)))
        # A sleep alone (CLOCK_MONOTONIC) never expires a write.
        self.clock.advance(30)
        self.event(1)
        self.channels.wait_for("frame")
        self.assertFalse(run.link.retired)
        awake.advance(5)
        self.assert_down(run, LinkEnd.WRITER_FAILED)


class ChannelsThroughLinkTest(LinkCase):
    def setUp(self):
        super().setUp()
        self.channel_targets: list[GatedSocket] = []
        self.relay = RelayChannels(self.mac.config, reader=self.mac.kernel, target_factory=self.gated_socket)
        self.relay.start()
        self.addCleanup(self.relay.close)

    def gated_socket(self, sock) -> GatedSocket:
        target = GatedSocket(sock)
        self.channel_targets.append(target)
        return target

    def registered(self) -> tuple[Run, ChannelClient]:
        run = self.start(channels=self.relay)
        self.hub.handshake()
        self.round_trip(1)
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(client=self.client()), daemon=True)
        thread.start()
        self.read_until(LinkType.REGISTER)
        self.hub.send(LinkType.REGISTERED, chan=1, generation=7)
        thread.join(GENEROUS)
        return run, holder["client"]

    def test_channel_write_spanning_sleep_decided_by_writer(self):
        # The clock jumps while a channel write is in progress: the channel
        # writer finishes the event on awake time, its outcome comes from the
        # bytes accepted (written: no undeliverable, no unregistered), and
        # the jump on CLOCK_MONOTONIC expires nothing.
        run, client = self.registered()
        self.channel_targets[0].hold(keep=10)
        self.event(1)
        self.assertTrue(self.channel_targets[0].held.wait(GENEROUS))
        self.clock.advance(30)
        self.channel_targets[0].release()
        kind, fields = client.read()
        self.assertEqual((kind, fields.get("request_id")), (MessageType.EVENT, request_id(1)))
        before = self.round_trip(2)
        self.assertNotIn(LinkType.UNDELIVERABLE, [kind for kind, _ in before])
        self.assertNotIn(LinkType.UNREGISTERED, [kind for kind, _ in before])
        self.assertFalse(run.link.retired)

    def client(self) -> ChannelClient:
        client = ChannelClient(self.mac.config.channel_socket, SESSION_A)
        self.addCleanup(client.close)
        return client

    def test_channel_registers_over_the_link(self):
        run = self.start(channels=self.relay)
        self.hub.handshake()
        self.round_trip(1)
        holder = {}
        thread = threading.Thread(target=lambda: holder.update(client=self.client()), daemon=True)
        thread.start()
        register = self.read_until(LinkType.REGISTER)[0]
        self.assertEqual((register["chan"], register["session_id"]), (1, SESSION_A))
        self.hub.send(LinkType.REGISTERED, chan=1, generation=7)
        thread.join(GENEROUS)
        self.assertEqual(holder["client"].first, (MessageType.WELCOME, {"v": PROTOCOL_VERSION, "type": "welcome",
                                                                          "epoch": EPOCH, "generation": 7}))
        self.hub.close()
        self.assertEqual(run.ended().code, LinkEnd.LINK_CLOSED)
        self.assertEqual(holder["client"].read(), (None, {}))

    def test_version_mismatch_refuses_channel_hellos(self):
        run = self.start(channels=self.relay)
        self.hub.accept()
        self.hub.expect(LinkType.LINK_HELLO)
        self.hub.send(LinkType.LINK_REFUSED, reason="version", versions=[2])
        self.assertEqual(run.ended().code, RefusedCode.VERSION)
        self.assertEqual(self.client().first[1].get("reason"), RelayRefusal.LINK_VERSION.value)
        # The next attempt's end says what holds then.
        self.hub.close()
        self.mac.config.link_socket.unlink()
        self.assertEqual(self.start(channels=self.relay).ended().code, RefusedCode.HUB_NOT_RUNNING)
        self.assertEqual(self.client().first[1].get("reason"), RelayRefusal.LINK_DOWN.value)


class RealHubTest(LinkHubCase, HubCase):
    """The real hub, the real pump and the real ssh-keygen on both ends."""

    real_verifier = True

    def relay(self, **options) -> Run:
        self.channels = FakeChannels()
        invocation = pump_invocation(self.home, key=self.key("mac").path)
        run = Run(RelayLink(self.channels, invocation, machine=MACHINE, **options))
        self.addCleanup(run.stop)
        return run

    def test_relay_admitted_by_the_real_hub(self):
        run = self.relay()
        self.channels.wait_for("up")
        wait_until(lambda: self.links().get("mac", {}).get("state") == "up", timeout=GENEROUS)
        self.assertEqual(run.link.epoch, self.hub.epoch)
        run.link.retire("test")
        self.assertEqual(run.ended().code, LinkEnd.RETIRED)
        wait_until(lambda: self.links()["mac"]["state"] == "down", timeout=GENEROUS)

    def assert_version_refused(self, run: Run, relay_versions: list[int], hub_versions: tuple[int, ...]):
        attempt = run.ended()
        self.assertEqual((attempt.code, attempt.hub_versions), (RefusedCode.VERSION, hub_versions))
        # Counted unattributed with the relay's list; no link state for the host.
        self.assertEqual(self.refusals()["version"]["versions"], relay_versions)
        self.assertNotEqual(self.links().get("mac", {}).get("state"), "up")
        # Channels are refused with the mismatch reason, and the next try is 10 minutes away.
        self.assertEqual(self.channels.calls, [("down", run.link, RelayRefusal.LINK_VERSION)])
        self.assertEqual(Backoff().after(attempt), FIXED_BACKOFF_SECONDS)

    def test_relay_v2_refused_and_counted_unattributed(self):
        self.assert_version_refused(self.relay(versions=(2,)), [2], (1,))

    def test_relay_v1_refused_by_hub_v2_and_counted_unattributed(self):
        with mock.patch.object(link_admission, "LINK_VERSIONS", (2,)):
            self.assert_version_refused(self.relay(), [1], (2,))


if __name__ == "__main__":
    unittest.main()
