"""The hub's side of the Mac link (docs/voice-desk.md, "Admission and proof
of possession", "Framing", "Deadlines, heartbeats and backpressure", "Link
objects, replacement and the fence on old work"): a real hub driven over
its `link.sock` by the test-side relay in tests/link_peer.py."""

import contextlib
import os
import socket
import threading
import unittest

from desk_fixtures import SESSION_A, Clock, HubCase, wait_until
from link_peer import MAC, MAC2, MACHINE, OTHER_MACHINE, LinkHubCase, LinkPeer, fake_signature, write_signers

from llm_switchboard.desk.link_admission import AdmissionRefusals
from llm_switchboard.desk.link_proof import ProofCheck, Signer, proof_message, verify_proof
from llm_switchboard.desk.link_protocol import (
    MAX_LINK_LINE_BYTES, LinkType, RefusedCode, Side, encode_frame,
)

SESSION_M = "dddddddd-0000-4000-8000-000000000004"


class RealProofTest(LinkHubCase, HubCase):
    real_verifier = True

    def test_good_proof_admits(self):
        peer = self.connect("mac")
        self.assertRegex(peer.link_id, "^[0-9a-f]{32}$")
        link = self.links()[MAC]
        self.assertEqual((link["state"], link["signer"]), ("up", True))
        self.assertIsNotNone(link["since"])

    def _refused(self, peer: LinkPeer, sign) -> dict:
        kind, challenge = peer.hello()
        self.assertIs(kind, LinkType.LINK_CHALLENGE)
        peer.version = challenge["version"]
        message = proof_message(version=challenge["version"], host=peer.host, machine=peer.machine,
                                epoch=challenge["epoch"], nonce=challenge["nonce"])
        peer.send(LinkType.LINK_PROOF, signature=sign(message, challenge))
        kind, fields = peer.read()
        self.assertIs(kind, LinkType.LINK_REFUSED)
        self.assertTrue(peer.closed())
        return fields

    def test_link_without_the_listed_key_refused(self):
        # An unrelated key, and another host's listed key, both signing as `mac`.
        for name in ("stranger", "mac2"):
            fields = self._refused(self.link_peer(), lambda message, _: self.key(name).sign(message))
            self.assertEqual(fields["reason"], RefusedCode.PROOF_FAILED)
        # A host with no line at all is refused before any challenge.
        kind, fields = self.link_peer(host="nobody").hello()
        self.assertEqual((kind, fields["reason"]), (LinkType.LINK_REFUSED, RefusedCode.NOT_AUTHORIZED))
        self.assertNotIn("nobody", self.links())
        self.assertEqual(self.refusals()[RefusedCode.PROOF_FAILED]["count"], 2)

    def test_proof_for_another_host_refused(self):
        # mac2's own key, over a message made for mac2, sent in a hello for mac.
        def sign(message, challenge):
            other = proof_message(version=challenge["version"], host=MAC2, machine=MACHINE,
                                  epoch=challenge["epoch"], nonce=challenge["nonce"])
            return self.key("mac2").sign(other)
        self.assertEqual(self._refused(self.link_peer(), sign)["reason"], RefusedCode.PROOF_FAILED)

    def test_old_nonce_replay_refused(self):
        first = self.link_peer()
        seen = {}

        def keep(message):
            seen["signature"] = self.key("mac").sign(message)
            return seen["signature"]
        self.assertEqual(first.handshake(keep)["type"], LinkType.LINK_WELCOME.value)
        fields = self._refused(self.link_peer(), lambda message, _: seen["signature"])
        self.assertEqual(fields["reason"], RefusedCode.PROOF_FAILED)

    def test_proof_bound_to_epoch_version_and_machine(self):
        changes = ({"epoch": "0" * 32}, {"machine": OTHER_MACHINE}, {"version": 2})
        for change in changes:
            def sign(message, challenge, change=change):
                fields = {"version": challenge["version"], "host": MAC, "machine": MACHINE,
                          "epoch": challenge["epoch"], "nonce": challenge["nonce"], **change}
                return self.key("mac").sign(proof_message(**fields))
            self.assertEqual(self._refused(self.link_peer(), sign)["reason"], RefusedCode.PROOF_FAILED, change)

    def test_verifier_gets_only_validated_signer_line(self):
        # verify_proof builds the message from the signer line's host, so a
        # proof is checked against exactly that host's validated line.
        pair = self.key("mac")
        message = proof_message(version=1, host=MAC, machine=MACHINE, epoch="1" * 32, nonce="2" * 64)
        signature = pair.sign(message)
        good = dict(version=1, machine=MACHINE, epoch="1" * 32, nonce="2" * 64, signature=signature)
        self.assertIs(verify_proof(pair.signer, **good), ProofCheck.PROOF_OK)
        self.assertIs(verify_proof(Signer(MAC2, pair.signer.blob), **good), ProofCheck.PROOF_BAD)


class PublicationTest(LinkHubCase, HubCase):
    def setUp(self):
        def verify(signer, **fields):
            result = verify_proof(signer, **fields)
            write_signers(self.config.link_signers_file, self.key("mac2").signer, *self.replacement)
            return result
        self.link_options = {"verify": verify}
        super().setUp()

    def test_publication_rechecks_signer_line(self):
        # The line is revoked, or rotated, between the verify and publication.
        for self.replacement in ((), (self.key("mac-rotated").signer,)):
            write_signers(self.config.link_signers_file, self.key("mac").signer, self.key("mac2").signer)
            peer = self.link_peer()
            fields = peer.handshake(self.key("mac"))
            self.assertEqual((fields["type"], fields["reason"]), (LinkType.LINK_REFUSED, RefusedCode.NOT_AUTHORIZED))
            self.assertTrue(peer.closed())
            self.assertNotEqual(self.links().get(MAC, {}).get("state"), "up")


class OtherUidTest(LinkHubCase, HubCase):
    link_options = {"peer_uid": lambda sock: os.getuid() + 1}

    def test_other_uid_refused(self):
        peer = self.link_peer()
        peer.send(LinkType.LINK_HELLO, versions=[1], relay_build="", host=MAC, machine=MACHINE)
        self.assertTrue(peer.closed())
        self.assertEqual(self.refusals(), {})
        self.assertNotEqual(self.links()[MAC]["state"], "up")


class AdmissionTest(LinkHubCase, HubCase):
    def test_version_refusal_counted_unattributed(self):
        self.connect()
        since = self.links()[MAC]["since"]
        kind, fields = self.link_peer().hello(versions=[2, 3])
        self.assertEqual((kind, fields["reason"], fields["versions"]), (LinkType.LINK_REFUSED, "version", [1]))
        self.assertEqual(self.links()[MAC], {"host": MAC, "state": "up", "since": since, "signer": True})
        entry = self.refusals()["version"]
        self.assertEqual((entry["count"], entry["versions"]), (1, [2, 3]))

    def test_unattributed_refusals_bounded(self):
        for n in range(12):
            kind, fields = self.link_peer(host=f"host{n}").hello()
            self.assertEqual(fields["reason"], RefusedCode.NOT_AUTHORIZED)
        self.assertEqual(set(self.refusals()), {"not_authorized"})
        self.assertEqual(self.refusals()["not_authorized"]["count"], 12)
        self.assertEqual(set(self.links()), {MAC, MAC2})
        # The record holds one entry per code whatever names arrive.
        record = AdmissionRefusals(Clock())
        for n in range(1000):
            record.add(RefusedCode.NOT_AUTHORIZED if n % 2 else RefusedCode.VERSION, [n % 7 + 2])
        self.assertEqual([entry["code"] for entry in record.view()], ["not_authorized", "version"])

    def test_refusal_versions_validated_and_capped(self):
        peer = self.link_peer()
        peer.send_raw(b'{"type":"link_hello","versions":[2,3,4,5,6],"relay_build":"","host":"mac","machine":"'
                      + MACHINE.encode() + b'"}\n')
        self.assertTrue(peer.closed())
        self.assertEqual(self.refusals(), {})
        kind, fields = self.link_peer().hello(versions=[2, 3, 4, 5])
        self.assertEqual(self.refusals()["version"]["versions"], [2, 3, 4, 5])

    def test_admission_timeout(self):
        self.hub.links.admission_seconds = 0.5
        peer = self.link_peer()
        kind, fields = peer.read()
        self.assertEqual((kind, fields["reason"]), (LinkType.LINK_REFUSED, RefusedCode.ADMISSION_TIMEOUT))

    def test_pending_admissions_capped(self):
        held = [self.link_peer(), self.link_peer()]
        for peer in held:
            # Each challenge witnesses a pending admission, without submitting its proof.
            self.assertIs(peer.hello()[0], LinkType.LINK_CHALLENGE)
        third = self.link_peer()
        self.assertTrue(third.closed())
        for peer in held:
            peer.close()

    def test_accept_rate_capped(self):
        for _ in range(20):
            peer = self.link_peer()
            peer.send_raw(b"not json\n")
            self.assertTrue(peer.closed())
        late = self.link_peer()
        late.send(LinkType.LINK_HELLO, versions=[1], relay_build="", host=MAC, machine=MACHINE)
        self.assertTrue(late.closed())
        self.assertEqual(self.refusals(), {})

    def test_admissions_per_host_capped(self):
        for _ in range(6):
            self.connect()
        refused = self.link_peer().handshake(lambda message: fake_signature(self.key("mac").signer))
        self.assertEqual((refused["type"], refused["reason"]), ("link_refused", "too_many_reconnects"))
        self.assertEqual(self.links()[MAC]["state"], "up")
        self.connect("mac2", MAC2)


class VerifierUnavailableTest(LinkHubCase, HubCase):
    verifier_ok = False

    def test_verifier_unavailable_refuses_links_keeps_pi(self):
        kind, fields = self.link_peer().hello()
        self.assertEqual(fields["reason"], RefusedCode.VERIFIER_UNAVAILABLE)
        self.assertEqual({view["state"] for view in self.links().values()}, {"verifier_unavailable"})
        self.assertEqual(self.channel(SESSION_A).first[0].value, "welcome")


class LinkObjectTest(LinkHubCase, HubCase):
    def setUp(self):
        super().setUp()
        self.remote[SESSION_M] = MAC
        self.hub.refresh_files()

    def test_replacement_retires_old_link_and_spares_successor(self):
        old = self.connect()
        self.assertEqual(old.register(1, SESSION_M)["type"], "registered")
        new = self.connect()
        self.assertTrue(old.closed())
        self.assertEqual(new.register(1, SESSION_M)["type"], "registered")
        # The old reader's finalizer ran; the successor's registration stands.
        wait_until(lambda: len(self.hub.state.registrations()) == 1)
        registration = self.hub.state.registrations()[0]
        self.assertEqual(registration.identity.link_id, new.link_id)
        self.assertIsNone(self.hub._recheck(registration.identity))

    def test_old_reader_finalizer_spares_successor(self):
        old = self.connect()
        old.register(1, SESSION_M)
        old_link = self.hub.links.live_link(MAC)
        new = self.connect()
        new.register(5, SESSION_M)
        self.hub.links._finalize(old_link)
        self.assertEqual([r.identity.link_id for r in self.hub.state.registrations()], [new.link_id])

    def test_unknown_chan_closes_link(self):
        peer = self.connect()
        peer.send(LinkType.DELIVERED, chan=7, request_id=f"{'e' * 32}.{'1' * 16}")
        self.assertTrue(peer.closed())
        wait_until(lambda: self.links()[MAC]["state"] == "down")

    def test_register_reusing_chan_closes_link(self):
        peer = self.connect()
        peer.register(3, SESSION_M)
        peer.send(LinkType.REGISTER, chan=2, session_id=SESSION_M, channel_pid=1, channel_start="1",
                  claude_pid=1, claude_start="1", label="", cwd="")
        self.assertTrue(peer.closed())

    def test_late_frames_on_a_dropped_chan_ignored(self):
        peer = self.connect()
        peer.register(1, SESSION_M)
        peer.send(LinkType.UNREGISTERED, chan=1, reason="channel_closed")
        wait_until(lambda: not self.hub.state.registrations())
        request_id = f"{self.hub.epoch}.{'1' * 16}"
        for kind, fields in ((LinkType.UNREGISTERED, {"reason": "channel_idle"}),
                             (LinkType.DELIVERED, {"request_id": request_id}),
                             (LinkType.UNDELIVERABLE, {"request_id": request_id, "reason": "channel_closed"}),
                             (LinkType.REPLY, {"request_id": request_id, "text": "t", "spoken": "s"})):
            peer.send(kind, chan=1, **fields)
        self.assertEqual(peer.register(2, SESSION_M)["type"], "registered")

    def test_query_id_above_highest_issued_closes_link(self):
        peer = self.connect()
        peer.send(LinkType.PRESENCE, query_id=1, state="gone")
        self.assertTrue(peer.closed())

    def test_pong_for_no_ping_and_bad_shapes_close_link(self):
        for frame in ({"kind": LinkType.PONG, "n": 3}, None):
            peer = self.connect()
            if frame:
                peer.send(frame.pop("kind"), **frame)
            else:
                peer.send_raw(b'{"type":"register","v":1,"chan":0}\n')
            self.assertTrue(peer.closed())

    def test_line_exactly_at_cap_passes_and_one_over_closes(self):
        peer = self.connect()
        peer.register(1, SESSION_M)
        request_id = f"{self.hub.epoch}.{'1' * 16}"
        base = len(encode_frame(LinkType.REPLY, Side.RELAY, v=1, chan=1, request_id=request_id, text="",
                                spoken=""))
        text = "x" * (MAX_LINK_LINE_BYTES - base)
        line = encode_frame(LinkType.REPLY, Side.RELAY, v=1, chan=1, request_id=request_id, text=text, spoken="")
        self.assertEqual(len(line), MAX_LINK_LINE_BYTES)
        peer.send_raw(line)
        result = peer.expect(LinkType.REPLY_RESULT)
        self.assertEqual((result["ok"], result["reason"]), (False, "too_large"))
        with contextlib.suppress(OSError):  # the hub closes before the line ends
            peer.send_raw(b"{" + b" " * MAX_LINK_LINE_BYTES + b"}\n")
        self.assertTrue(peer.closed())

    def test_register_refusals_keep_link(self):
        peer = self.connect()
        self.assertEqual(peer.register(1, SESSION_A)["reason"], "enrolled_on_another_host")
        self.assertEqual(peer.register(2, "eeeeeeee-0000-4000-8000-000000000005")["reason"], "not_enrolled")
        self.remote["eeeeeeee-0000-4000-8000-000000000005"] = MAC2
        self.hub.refresh_files()
        self.assertEqual(peer.register(3, "eeeeeeee-0000-4000-8000-000000000005")["reason"],
                         "enrolled_on_another_host")
        sessions = [f"{n:08x}-0000-4000-8000-{n:012x}" for n in range(16)]
        self.remote.update({s: MAC for s in sessions})
        self.hub.refresh_files()
        for chan, session in enumerate(sessions, start=10):
            self.assertEqual(peer.register(chan, session)["type"], "registered")
        self.assertEqual(peer.register(40, SESSION_M)["reason"], "too_many_registrations")
        self.assertEqual(self.links()[MAC]["state"], "up")


class SignerCheckTest(LinkHubCase, HubCase):
    def test_signer_removal_retires_established_link_without_link_drop(self):
        # The hub never receives link_drop: the housekeeping pass's first
        # step, the signer check, retires the link.
        mac, mac2 = self.connect(), self.connect("mac2", MAC2)
        write_signers(self.config.link_signers_file, self.key("mac2").signer)
        self.hub.recheck_now()
        self.assertTrue(mac.closed())
        wait_until(lambda: (self.links()[MAC]["state"], self.links()[MAC]["signer"]) == ("down", False))
        self.assertEqual(self.links()[MAC2]["state"], "up")

    def test_rotation_retires_old_key_link(self):
        mac = self.connect()
        write_signers(self.config.link_signers_file, self.key("mac-rotated").signer, self.key("mac2").signer)
        answer = self.hub.links.check_signers(MAC)
        self.assertTrue(mac.closed())
        self.assertEqual(answer["signer"]["fingerprint"], self.key("mac-rotated").signer.fingerprint)
        self.assertIsNone(answer["link"])
        self.connect("mac-rotated")

    def test_unreadable_signers_retires_all_links(self):
        mac, mac2 = self.connect(), self.connect("mac2", MAC2)
        self.config.link_signers_file.write_text("garbage\n")
        self.assertFalse(self.hub.links.check_signers()["readable"])
        self.assertTrue(mac.closed() and mac2.closed())
        kind, fields = self.link_peer().hello()
        self.assertEqual(fields["reason"], RefusedCode.SIGNERS_UNREADABLE)


class HeartbeatTest(LinkHubCase, HubCase):
    def setUp(self):
        self.clock = Clock(1000.0)
        self.link_options = {"clock": self.clock, "tick_seconds": 3600}
        super().setUp()

    def test_ping_after_quiet_and_idle_retires(self):
        peer = self.connect()
        self.clock.advance(15)
        self.hub.links.tick()
        kind, ping = peer.read(answer_pings=False)
        self.assertEqual(kind, LinkType.PING)
        peer.send(LinkType.PONG, n=ping["n"])
        wait_until(lambda: self.hub.links.live_link(MAC).last_received == self.clock.now)
        self.clock.advance(44)
        self.hub.links.tick()
        self.assertEqual(self.links()[MAC]["state"], "up")
        self.clock.advance(1)
        self.hub.links.tick()
        self.assertTrue(peer.closed())

    def test_hub_answers_ping(self):
        peer = self.connect()
        peer.send(LinkType.PING, n=1)
        self.assertEqual(peer.expect(LinkType.PONG), {"n": 1})


if __name__ == "__main__":
    unittest.main()
