"""A revoke reports itself complete only once no startup acknowledgement can
still press its Enter on a revoked token: it waits for acknowledgements in
flight (`token_in_use`) even when an earlier revoke already deleted the
token, and when its bounded wait runs out it says so, through the state,
the operator reply and `desk revoke`'s text and exit code."""

import hashlib
import secrets
import threading
import time
import unittest

from desk_fixtures import Clock, HubCase, desk_config, make_home, run_desk_cli

from llm_switchboard.desk.cli import EXIT_UNKNOWN
from llm_switchboard.desk.hub_state import HubState
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import REVOKE_IN_PROGRESS, Revoked


class StateRevokeTest(unittest.TestCase):
    def setUp(self):
        config = desk_config(make_home(self))
        self.state = HubState(LockStore(config.lock_file), now=Clock(), elapsed=Clock(), token_bytes=secrets.token_bytes)
        self.token = self.new_token()

    def new_token(self) -> str:
        """Paired the way the tablet pairs."""
        secret = secrets.token_hex(32)
        started = self.state.pair_start(hashlib.sha256(secret.encode()).hexdigest())
        self.state.pair_confirm(started["code"])
        return self.state.pair_claim(started["pair_id"], secret)["token"]

    def test_a_wait_that_runs_out_is_not_reported_settled(self):
        self.state.revoke_wait_seconds = 0.2
        with self.state.token_in_use(self.token):
            self.assertEqual(self.state.revoke(), Revoked(1, settled=False))
        self.assertFalse(self.state.authenticate(self.token))
        # With nothing in flight any more, a revoke is settled at once.
        self.assertEqual(self.state.revoke(), Revoked(0, settled=True))

    def test_a_second_revoke_during_the_first_ones_wait_also_waits(self):
        token_id = self.state.status()["tokens"][0]["token_id"]
        results = {}
        held = threading.Event()
        release = threading.Event()

        def acknowledgement():
            with self.state.token_in_use(self.token):
                held.set()
                release.wait(5)
        ack = threading.Thread(target=acknowledgement)
        ack.start()
        held.wait(5)

        def revoke(name, **kwargs):
            results[name] = (self.state.revoke(**kwargs), time.monotonic())
        first = threading.Thread(target=revoke, args=("first",))
        first.start()
        time.sleep(0.2)
        self.assertNotIn("first", results)
        # The first revoke deleted the token; the second still finds the
        # acknowledgement on it, by id and when revoking all.
        seconds = [threading.Thread(target=revoke, args=("by id",), kwargs={"token_id": token_id}),
                   threading.Thread(target=revoke, args=("all",))]
        for thread in seconds:
            thread.start()
        time.sleep(0.2)
        self.assertEqual(results, {})
        released_at = time.monotonic()
        release.set()
        for thread in (ack, first, *seconds):
            thread.join(10)
        self.assertEqual({name: result for name, (result, _) in results.items()},
                         {"first": Revoked(1, True), "by id": Revoked(0, True), "all": Revoked(0, True)})
        self.assertTrue(all(at >= released_at for _, at in results.values()))

    def test_another_tokens_acknowledgement_does_not_hold_a_revoke_by_id(self):
        other = self.new_token()
        ids = [t["token_id"] for t in self.state.status()["tokens"]]
        self.state.revoke_wait_seconds = 0.2
        with self.state.token_in_use(other):
            started = time.monotonic()
            self.assertEqual(self.state.revoke(ids[0]), Revoked(1, settled=True))
            self.assertLess(time.monotonic() - started, 0.15)


class OperatorRevokeTest(HubCase):
    def test_the_operator_reply_and_the_cli_say_an_acknowledgement_is_still_running(self):
        token = self.pair()
        token_id = self.hub.state.status()["tokens"][0]["token_id"]
        self.hub.state.revoke_wait_seconds = 0.3
        with self.hub.state.token_in_use(token):
            self.assertEqual(self.hub.handle_operator({"op": "revoke"}),
                             {"ok": False, "error": REVOKE_IN_PROGRESS, "revoked": 1, "in_progress": True})
            for args, again in (((), "llm-switchboard desk revoke"),
                                (("--token", token_id), f"llm-switchboard desk revoke --token {token_id}")):
                result = run_desk_cli(self.home, "revoke", *args)
                self.assertEqual((result.returncode, result.stdout), (EXIT_UNKNOWN, ""))
                self.assertEqual(result.stderr.strip(),
                                 f"desk: {REVOKE_IN_PROGRESS}; run `{again}` again to wait for it")
        result = run_desk_cli(self.home, "revoke")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertIn('"revoked": 0', result.stdout)


if __name__ == "__main__":
    unittest.main()
