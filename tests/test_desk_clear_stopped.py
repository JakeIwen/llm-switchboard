"""The operator-only override for a Mac lock whose Mac can't answer
(docs/voice-desk.md, "Hub side": operator-only override, no offline form):
`desk clear-stopped` and the operator op `clear_stopped`, with a real hub
driven by the test-side relay in tests/link_peer.py."""

import threading

from desk_fixtures import SESSION_A, desk_config, make_home, run_desk_cli, wait_until
from link_peer import MAC, OTHER_MACHINE
from test_desk_host_enrollment import mac_lock
from test_desk_link_channels import SESSION_M, MacTargetCase

from llm_switchboard.desk import http_server
from llm_switchboard.desk.cli import CLEAR_STOPPED_STATEMENT
from llm_switchboard.desk.link_protocol import LinkType
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import DeskError
from llm_switchboard.desk.refusals import Refusal


class ClearStoppedCase(MacTargetCase):
    def stopped_lock(self) -> dict:
        """A Mac lock whose link then went away."""
        self.send_m()
        self.mac.expect(LinkType.EVENT)
        self.mac.close()
        wait_until(lambda: self.links()[MAC]["state"] == "down", timeout=30)
        return self.lock_m()

    def op(self, lock_id: str, confirm=None, session_id: str = SESSION_M) -> dict:
        message = {"op": "clear_stopped", "session_id": session_id, "lock_id": lock_id}
        if confirm is not None:
            message["confirm"] = confirm
        return self.hub.handle_operator(message)

    def cli(self, *args):
        return run_desk_cli(self.home, "clear-stopped", SESSION_M, *args, input="")

    def persisted(self) -> list[str]:
        return [lock.lock_id for lock in LockStore(self.config.lock_file).load()]


class ClearStoppedTest(ClearStoppedCase):
    def test_clear_stopped_requires_confirm_equal_to_lock_id(self):
        lock_id = self.stopped_lock()["lock_id"]
        for confirm in (None, "", SESSION_M[:8], lock_id.upper(), lock_id + " "):
            with self.subTest(confirm=confirm):
                self.assertEqual(self.op(lock_id, confirm), {"ok": False,
                                                             "error": Refusal.CLEAR_STOPPED_NEEDS_CONFIRM})
        self.assertEqual(self.persisted(), [lock_id])
        # The command: off a terminal it needs --confirm, and it must be the lock id.
        done = self.cli(lock_id)
        self.assertEqual(done.returncode, 1)
        self.assertIn("--confirm", done.stderr)
        done = self.cli(lock_id, "--confirm", SESSION_M[:8])
        self.assertEqual(done.returncode, 1)
        self.assertIn(Refusal.CLEAR_STOPPED_NEEDS_CONFIRM, done.stderr)
        self.assertEqual(self.persisted(), [lock_id])
        done = self.cli(lock_id, "--confirm", lock_id)
        self.assertEqual(done.returncode, 0, done.stderr)
        # It showed the lock and the statement it records before acting.
        for shown in (MAC, self.key("mac").signer.fingerprint, "abababab", CLEAR_STOPPED_STATEMENT):
            self.assertIn(shown, done.stdout)
        self.assertEqual(self.persisted(), [])
        self.assertIsNone(self.target(SESSION_M)["lock"])

    def test_clear_stopped_refused_while_matching_link_up(self):
        lock_id = self.lock_m_after_send()
        self.assertEqual(self.op(lock_id, lock_id)["error"], Refusal.CLEAR_STOPPED_LINK_UP)
        self.assertEqual(self.persisted(), [lock_id])
        # A Pi lock is never this command's.
        channel = self.channel(SESSION_A)
        ticket = self.hub.ticket({"session_id": SESSION_A, "generation": channel.first[1]["generation"],
                                  "epoch": self.hub.epoch}, self.token)["ticket"]
        self.hub.send({"ticket": ticket, "text": "x"}, self.token)
        pi_lock = self.target(SESSION_A)["lock"]["lock_id"]
        answer = self.op(pi_lock, pi_lock, session_id=SESSION_A)
        self.assertEqual(answer["error"], Refusal.CLEAR_STOPPED_PI_LOCK)
        # The Mac replaced: a link for the host with another machine doesn't
        # answer for the lock, so the override applies.
        self.mac.close()
        wait_until(lambda: self.links()[MAC]["state"] == "down", timeout=30)
        self.connect(machine=OTHER_MACHINE)
        self.assertEqual(self.op(lock_id, lock_id), {"ok": True, "cleared": True})
        self.assertEqual(self.persisted(), [pi_lock])

    def lock_m_after_send(self) -> str:
        self.send_m()
        self.mac.expect(LinkType.EVENT)
        return self.lock_m()["lock_id"]

    def test_clear_stopped_rechecks_lock_at_mutation(self):
        lock_id = self.stopped_lock()["lock_id"]
        # What the command saw (the lock, its link down) no longer holds at
        # the mutation: the Mac reconnected with the lock's key and machine.
        [row] = [row for row in self.hub.handle_operator({"op": "status"})["locks"] if row["lock_id"] == lock_id]
        self.assertEqual(row["host"], MAC)
        self.mac = self.connect()
        self.assertEqual(self.op(lock_id, lock_id)["error"], Refusal.CLEAR_STOPPED_LINK_UP)
        self.mac.close()
        wait_until(lambda: self.links()[MAC]["state"] == "down", timeout=30)
        # Two overrides at once: the check and the removal are one hold of the
        # state's lock, so the second finds the lock gone.
        inside, resume = threading.Event(), threading.Event()
        real = self.hub.links.link_for_lock

        def paused(lock):
            inside.set()
            resume.wait(30)
            return real(lock)
        self.hub.links.link_for_lock = paused
        self.addCleanup(setattr, self.hub.links, "link_for_lock", real)
        answers = {}
        first = threading.Thread(target=lambda: answers.setdefault("first", self.op(lock_id, lock_id)), daemon=True)
        first.start()
        self.assertTrue(inside.wait(30))
        self.hub.links.link_for_lock = real
        second = threading.Thread(target=lambda: answers.setdefault("second", self.op(lock_id, lock_id)),
                                  daemon=True)
        second.start()
        second.join(0.5)
        self.assertTrue(second.is_alive(), "the second override ran while the first held the state's lock")
        resume.set()
        first.join(30)
        second.join(30)
        self.assertEqual(answers["first"], {"ok": True, "cleared": True})
        self.assertEqual(answers["second"], {"ok": False, "error": Refusal.NO_LOCK})
        self.assertEqual(self.persisted(), [])

    def test_no_http_route_reaches_clear_stopped(self):
        routed = {method for method, _ in http_server.POST_ROUTES.values()}
        self.assertNotIn("clear_stopped", routed)
        self.assertFalse(any("stopped" in path for path in (*http_server.POST_ROUTES, *http_server.STATIC_ROUTES)))
        lock_id = self.stopped_lock()["lock_id"]
        token = self.pair()
        body = {"session_id": SESSION_M, "lock_id": lock_id, "confirm": lock_id}
        for path in ("/desk/api/clear-stopped", "/desk/api/clear_stopped", "/desk/api/clear/stopped"):
            with self.subTest(path=path):
                self.assertEqual(self.call("POST", path, body, token=token)[0], 404)
        # The page's clear asks the Mac, which isn't there: it never overrides.
        status, _, answer = self.call("POST", "/desk/api/clear", body, token=token)
        self.assertEqual((status, answer), (503, {"error": Refusal.MAC_NOT_CONNECTED}))
        self.assertEqual(self.persisted(), [lock_id])

    def test_clear_stopped_has_no_offline_form(self):
        home = make_home(self)
        config = desk_config(home)
        lock = mac_lock(config, SESSION_M, self.key("mac").signer.fingerprint)
        before = config.lock_file.read_bytes()
        done = run_desk_cli(home, "clear-stopped", SESSION_M, lock.lock_id, "--confirm", lock.lock_id, input="")
        self.assertEqual(done.returncode, 1)
        self.assertIn("the desk hub is not running; start it and run clear-stopped again", done.stderr)
        self.assertEqual(config.lock_file.read_bytes(), before)

    def test_clear_stopped_refuses_a_lock_in_delivery_and_bad_refs(self):
        lock_id = self.stopped_lock()["lock_id"]
        with self.assertRaises(DeskError) as caught:
            self.hub.state.clear_stopped(SESSION_M, None, None, lambda lock: False)
        self.assertEqual(caught.exception.reason, Refusal.BAD_LOCK_REF)
        self.assertEqual(self.op("0" * 16, "0" * 16)["error"], Refusal.LOCK_ID_MISMATCH)
        self.assertEqual(self.op(lock_id, lock_id, session_id=SESSION_A)["error"], Refusal.NO_LOCK)
        self.assertEqual(self.persisted(), [lock_id])
