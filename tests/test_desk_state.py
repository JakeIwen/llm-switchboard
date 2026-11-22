"""The hub's state machine through its public methods: tickets, sends,
handoff, replies, locks across restarts, pairing, tokens and caps."""

import hashlib
import json
import secrets
import unittest

from desk_fixtures import SESSION_A, SESSION_B, Clock, FakeIdentity, desk_config, make_home

from llm_switchboard.desk.hub_state import HubState
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import (
    MAX_LOCKS, MAX_PAIR_SECRET_FAILURES, MAX_REGISTRATIONS, MAX_REPLY_RING_BYTES, MAX_REQUESTS, MAX_TICKETS,
    MAX_TOKENS, PAIR_TTL_SECONDS, TICKET_TTL_SECONDS, TOKEN_TTL_SECONDS, DeskError, Lock, LockStoreError,
)
from llm_switchboard.desk.protocol import DELIVERY_SUFFIX, MAX_REPLY_TEXT_BYTES


def identity(session_id: str, pid: int = 100) -> FakeIdentity:
    return FakeIdentity(pid, "1", pid + 1, "2", session_id)


class FailingLockStore:
    def __init__(self):
        self.added = []

    def load(self):
        return []

    def add(self, lock):
        raise LockStoreError("lock file unwritable")

    def remove(self, lock_id):
        raise LockStoreError("lock file unwritable")


class StateCase(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.config = desk_config(self.home)
        self.clock = Clock()
        self.state = self.new_state()

    def new_state(self, store=None) -> HubState:
        state = HubState(store or LockStore(self.config.lock_file), now=self.clock, token_bytes=secrets.token_bytes)
        state.set_enrolled([SESSION_A, SESSION_B])
        state.set_sends_enabled(True)
        return state

    def assertRefused(self, status, reason, call, *args):
        with self.assertRaises(DeskError) as caught:
            call(*args)
        self.assertEqual((caught.exception.status, caught.exception.reason), (status, reason))

    def ticket(self, state, session_id, generation):
        return state.issue_ticket(session_id, generation, state.epoch)["ticket"]

    def handed_off(self, state, conn_id, session_id, generation, text="hello there"):
        request = state.begin_send(self.ticket(state, session_id, generation), text)
        conn, content = state.hand_off(request.request_id)
        self.assertEqual(conn, conn_id)
        return request.request_id, content

    def target(self, state, session_id):
        return next(t for t in state.snapshot()["targets"] if t["session_id"] == session_id)

    def lock_file_ids(self):
        if not self.config.lock_file.exists():
            return []
        return [lock["lock_id"] for lock in json.loads(self.config.lock_file.read_text())["locks"]]


class RegistrationTest(StateCase):
    def test_two_connections_for_one_session_are_ambiguous_and_both_unroutable(self):
        first = self.state.register(1, SESSION_A, identity(SESSION_A))
        second = self.state.register(2, SESSION_A, identity(SESSION_A, 200))
        self.assertEqual(self.target(self.state, SESSION_A)["registration"], "ambiguous")
        self.assertIsNone(self.target(self.state, SESSION_A)["generation"])
        for generation in (first, second):
            self.assertRefused(409, "ambiguous", self.state.issue_ticket, SESSION_A, generation, self.state.epoch)
        self.state.drop_registration(1)
        self.assertEqual(self.target(self.state, SESSION_A)["registration"], "registered")
        self.assertTrue(self.ticket(self.state, SESSION_A, second))

    def test_a_ticket_taken_before_ambiguity_cannot_send_during_it(self):
        generation = self.state.register(1, SESSION_A, identity(SESSION_A))
        ticket = self.ticket(self.state, SESSION_A, generation)
        self.state.register(2, SESSION_A, identity(SESSION_A, 200))
        self.assertRefused(409, "ambiguous", self.state.begin_send, ticket, "hi")

    def test_registrations_are_capped(self):
        for conn_id in range(MAX_REGISTRATIONS):
            self.state.register(conn_id, SESSION_A, identity(SESSION_A, conn_id))
        self.assertRefused(429, "too many registrations", self.state.register, 999, SESSION_B, identity(SESSION_B))

    def test_generations_increase_and_targets_cover_enrolled_and_registered(self):
        first = self.state.register(1, SESSION_A, identity(SESSION_A))
        self.state.drop_registration(1)
        second = self.state.register(2, SESSION_A, identity(SESSION_A))
        self.assertGreater(second, first)
        other = "dddddddd-0000-4000-8000-000000000004"
        self.state.register(3, other, identity(other))
        targets = {t["session_id"]: t for t in self.state.snapshot()["targets"]}
        self.assertEqual(set(targets), {SESSION_A, SESSION_B, other})
        self.assertFalse(targets[other]["enrolled"])
        self.assertEqual(targets[SESSION_B]["registration"], "none")


class TicketTest(StateCase):
    def setUp(self):
        super().setUp()
        self.generation = self.state.register(1, SESSION_A, identity(SESSION_A))

    def test_stale_epoch_and_generation_are_refused(self):
        self.assertRefused(409, "stale epoch", self.state.issue_ticket, SESSION_A, self.generation, "0" * 32)
        self.assertRefused(409, "stale generation", self.state.issue_ticket, SESSION_A, self.generation + 1,
                           self.state.epoch)
        ticket = self.ticket(self.state, SESSION_A, self.generation)
        self.state.drop_registration(1)
        self.state.register(2, SESSION_A, identity(SESSION_A))
        self.assertRefused(409, "stale generation", self.state.begin_send, ticket, "hi")

    def test_a_ticket_from_another_epoch_is_refused(self):
        other = self.new_state()
        other.register(1, SESSION_A, identity(SESSION_A))
        foreign = self.ticket(other, SESSION_A, 1)
        self.assertRefused(409, "stale epoch", self.state.begin_send, foreign, "hi")
        self.assertRefused(409, "unknown ticket", self.state.begin_send, self.state.epoch + ".nope", "hi")

    def test_tickets_are_one_use_and_a_delayed_duplicate_after_the_answer_is_refused(self):
        ticket = self.ticket(self.state, SESSION_A, self.generation)
        request = self.state.begin_send(ticket, "first")
        self.state.hand_off(request.request_id)
        self.assertIsNone(self.state.handle_reply(1, request.request_id, "done", "Done."))
        self.assertRefused(409, "ticket already used", self.state.begin_send, ticket, "first")

    def test_tickets_expire(self):
        ticket = self.ticket(self.state, SESSION_A, self.generation)
        self.clock.advance(TICKET_TTL_SECONDS)
        self.assertRefused(409, "ticket expired", self.state.begin_send, ticket, "hi")

    def test_a_full_table_refuses_new_tickets_and_keeps_consumed_records_until_expiry(self):
        consumed = self.ticket(self.state, SESSION_A, self.generation)
        request = self.state.begin_send(consumed, "hi")
        self.state.hand_off(request.request_id)
        self.state.handle_reply(1, request.request_id, "ok", "ok")
        live = [self.ticket(self.state, SESSION_A, self.generation) for _ in range(MAX_TICKETS - 1)]
        self.assertRefused(429, "ticket table full", self.state.issue_ticket, SESSION_A, self.generation,
                           self.state.epoch)
        # The refusal evicted nothing: the consumed record still refuses its
        # duplicate and a live ticket still works.
        self.assertRefused(409, "ticket already used", self.state.begin_send, consumed, "hi")
        pending = self.state.begin_send(live[0], "again")
        self.assertEqual(pending.state.value, "pending")
        self.state.expire_request(pending.request_id)
        self.clock.advance(TICKET_TTL_SECONDS)
        self.assertTrue(self.ticket(self.state, SESSION_A, self.generation))

    def test_text_rule_refusals_and_sends_off(self):
        ticket = self.ticket(self.state, SESSION_A, self.generation)
        self.assertRefused(400, "starts with a command character", self.state.begin_send, ticket, "/clear")
        self.assertRefused(400, "leading or trailing whitespace", self.state.begin_send, ticket, " hi")
        self.state.set_sends_enabled(False)
        self.assertRefused(403, "sends off", self.state.begin_send, ticket, "hi")
        self.state.set_sends_enabled(True)
        # Refused texts did not burn the ticket.
        self.assertEqual(self.state.begin_send(ticket, "hi").state.value, "pending")


class HandoffTest(StateCase):
    def setUp(self):
        super().setUp()
        self.generation = self.state.register(1, SESSION_A, identity(SESSION_A))

    def test_handoff_persists_the_lock_before_returning_the_event(self):
        request_id, content = self.handed_off(self.state, 1, SESSION_A, self.generation, "what changed?")
        self.assertEqual(content, "what changed?" + DELIVERY_SUFFIX.format(request_id=request_id))
        lock = self.target(self.state, SESSION_A)["lock"]
        self.assertEqual(self.lock_file_ids(), [lock["lock_id"]])
        saved = json.loads(self.config.lock_file.read_text())["locks"][0]
        self.assertEqual(set(saved), {"lock_id", "host", "session_id", "request_id", "ticket_hash", "since"})
        self.assertNotIn("what changed", self.config.lock_file.read_text())
        self.assertRefused(409, "busy", self.state.issue_ticket, SESSION_A, self.generation, self.state.epoch)

    def test_crash_after_the_lock_and_before_the_write_leaves_the_target_busy(self):
        request_id, _ = self.handed_off(self.state, 1, SESSION_A, self.generation)
        # The hub dies here, before writing the event. A new run reads the file.
        restarted = self.new_state()
        generation = restarted.register(1, SESSION_A, identity(SESSION_A))
        lock = self.target(restarted, SESSION_A)["lock"]
        self.assertEqual(lock["request_id"], request_id)
        self.assertTrue(lock["earlier_run"])
        self.assertNotEqual(restarted.epoch, self.state.epoch)
        self.assertRefused(409, "busy", restarted.issue_ticket, SESSION_A, generation, restarted.epoch)

    def test_a_failing_lock_store_refuses_the_send_with_503_and_nothing_is_handed_off(self):
        state = self.new_state(FailingLockStore())
        generation = state.register(1, SESSION_A, identity(SESSION_A))
        request = state.begin_send(self.ticket(state, SESSION_A, generation), "hi")
        self.assertRefused(503, "lock file unwritable", state.hand_off, request.request_id)
        self.assertEqual(state.request_state(request.request_id), "expired")
        self.assertEqual(state.snapshot()["lock_store_error"], "lock file unwritable")
        self.assertRefused(503, "lock file unwritable", state.issue_ticket, SESSION_A, generation, state.epoch)

    def test_handoff_refuses_when_the_registration_changed_after_begin(self):
        request = self.state.begin_send(self.ticket(self.state, SESSION_A, self.generation), "hi")
        self.state.drop_registration(1)
        self.state.register(2, SESSION_A, identity(SESSION_A))
        self.assertRefused(409, "request not pending", self.state.hand_off, request.request_id)
        self.assertEqual(self.lock_file_ids(), [])

    def test_disconnect_after_handoff_makes_the_request_unknown_and_keeps_the_lock(self):
        request_id, _ = self.handed_off(self.state, 1, SESSION_A, self.generation)
        self.assertEqual(self.state.drop_registration(1), [request_id])
        self.assertEqual(self.state.request_state(request_id), "unknown")
        self.assertEqual(len(self.lock_file_ids()), 1)
        self.assertFalse(self.target(self.state, SESSION_A)["lock"]["earlier_run"])

    def test_restart_with_a_prompt_behind_a_dialog_needs_a_clear_naming_that_lock(self):
        first_id, _ = self.handed_off(self.state, 1, SESSION_A, self.generation)
        old_lock = self.target(self.state, SESSION_A)["lock"]["lock_id"]
        self.state.handle_reply(1, first_id, "ok", "ok")
        self.handed_off(self.state, 1, SESSION_A, self.generation)
        held_lock = self.target(self.state, SESSION_A)["lock"]["lock_id"]
        # Never answered (a permission dialog holds it); the hub restarts and
        # the same live session registers again.
        restarted = self.new_state()
        generation = restarted.register(7, SESSION_A, identity(SESSION_A))
        self.assertRefused(409, "busy", restarted.issue_ticket, SESSION_A, generation, restarted.epoch)
        self.assertRefused(409, "lock id does not match", restarted.clear_lock, SESSION_A, old_lock)
        self.assertRefused(409, "busy", restarted.issue_ticket, SESSION_A, generation, restarted.epoch)
        restarted.clear_lock(SESSION_A, held_lock)
        self.assertEqual(self.lock_file_ids(), [])
        self.assertRefused(409, "no lock", restarted.clear_lock, SESSION_A, held_lock)
        ticket = self.ticket(restarted, SESSION_A, generation)
        self.assertEqual(restarted.begin_send(ticket, "again").state.value, "pending")


class ReplyTest(StateCase):
    def setUp(self):
        super().setUp()
        self.generation = self.state.register(1, SESSION_A, identity(SESSION_A))
        self.other = self.state.register(2, SESSION_B, identity(SESSION_B))
        self.request_id, _ = self.handed_off(self.state, 1, SESSION_A, self.generation)

    def test_bad_replies_are_dropped_and_the_first_valid_one_wins(self):
        big = "x" * (MAX_REPLY_TEXT_BYTES + 1)
        cases = [
            ((1, self.state.epoch + ".0000", "a", "a"), "unknown request"),
            ((2, self.request_id, "a", "a"), "wrong connection"),
            ((1, self.request_id, big, "a"), "reply too large"),
            ((1, self.request_id, "a", "s" * 2000), "reply too large"),
            ((1, self.request_id, None, "a"), "bad reply"),
        ]
        for args, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.state.handle_reply(*args), reason)
        self.assertEqual(self.state.request_state(self.request_id), "handed_off")
        self.assertEqual(self.target(self.state, SESSION_A)["registration"], "registered")
        self.assertIsNone(self.state.handle_reply(1, self.request_id, "the answer", "Short answer."))
        self.assertEqual(self.state.handle_reply(1, self.request_id, "second", "second"), "request not outstanding")
        view = self.state.snapshot()
        [request] = [r for r in view["requests"] if r["request_id"] == self.request_id]
        self.assertEqual((request["state"], request["reply"]),
                         ("answered", {"text": "the answer", "spoken": "Short answer."}))
        self.assertIsNone(self.target(self.state, SESSION_A)["lock"])
        self.assertEqual(self.target(self.state, SESSION_A)["registration"], "verified")
        self.assertEqual(self.lock_file_ids(), [])

    def test_a_reply_after_disconnect_or_clear_is_dropped(self):
        lock_id = self.target(self.state, SESSION_A)["lock"]["lock_id"]
        self.state.clear_lock(SESSION_A, lock_id)
        self.assertEqual(self.state.handle_reply(1, self.request_id, "late", "late"), "request not outstanding")
        request_id, _ = self.handed_off(self.state, 2, SESSION_B, self.other)
        self.state.drop_registration(2)
        self.assertEqual(self.state.handle_reply(2, request_id, "late", "late"), "wrong connection")


class CapsTest(StateCase):
    def setUp(self):
        super().setUp()
        self.generation = self.state.register(1, SESSION_A, identity(SESSION_A))

    def answer(self, text="ok", spoken="ok"):
        request_id, _ = self.handed_off(self.state, 1, SESSION_A, self.generation)
        self.assertIsNone(self.state.handle_reply(1, request_id, text, spoken))
        # Keeps the consumed tickets from filling the ticket table.
        self.clock.advance(TICKET_TTL_SECONDS / 10)
        return request_id

    def test_requests_and_reply_bytes_are_bounded(self):
        for _ in range(MAX_REQUESTS + 5):
            self.answer()
        self.assertEqual(len(self.state.snapshot()["requests"]), MAX_REQUESTS)
        big = "y" * MAX_REPLY_TEXT_BYTES
        for _ in range(12):
            self.answer(big, "s")
        replies = [r["reply"] for r in self.state.snapshot()["requests"] if r["reply"]]
        self.assertLessEqual(sum(len(r["text"]) + len(r["spoken"]) for r in replies), MAX_REPLY_RING_BYTES)
        self.assertEqual(len(replies), MAX_REPLY_RING_BYTES // (MAX_REPLY_TEXT_BYTES + 1))

    def test_a_full_lock_file_refuses_every_send(self):
        locks = [Lock(f"{i:016x}", "pi", f"{i:08x}-0000-4000-8000-000000000000", f"old.{i}", "h", 1.0)
                 for i in range(MAX_LOCKS)]
        store = LockStore(self.config.lock_file)
        store.load()
        for lock in locks:
            store.add(lock)
        with self.assertRaises(LockStoreError):
            store.add(Lock("f" * 16, "pi", SESSION_B, "old.x", "h", 1.0))
        state = self.new_state()
        generation = state.register(1, SESSION_A, identity(SESSION_A))
        self.assertEqual(state.snapshot()["lock_store_error"], "lock file full")
        self.assertRefused(503, "lock file full", state.issue_ticket, SESSION_A, generation, state.epoch)


class PairingTest(StateCase):
    def start(self, secret=None):
        secret = secret or secrets.token_hex(32)
        return secret, self.state.pair_start(hashlib.sha256(secret.encode()).hexdigest())

    def test_confirmed_claim_hands_out_the_token_once(self):
        secret, started = self.start()
        self.assertRegex(started["code"], r"^[0-9A-HJKMNP-TV-Z]{3}-[0-9A-HJKMNP-TV-Z]{3}$")
        self.assertEqual(started["expires_in"], PAIR_TTL_SECONDS)
        self.assertRefused(409, "not confirmed", self.state.pair_claim, started["pair_id"], secret)
        self.state.pair_confirm(started["code"].lower())
        claimed = self.state.pair_claim(started["pair_id"], secret)
        self.assertTrue(self.state.authenticate(claimed["token"]))
        self.assertEqual(claimed["expires_at"], self.clock.now + TOKEN_TTL_SECONDS)
        self.assertRefused(404, "no such pairing", self.state.pair_claim, started["pair_id"], secret)
        self.assertNotIn(claimed["token"], json.dumps(self.state.status()))

    def test_wrong_secrets_kill_the_pairing(self):
        secret, started = self.start()
        self.state.pair_confirm(started["code"])
        for _ in range(MAX_PAIR_SECRET_FAILURES):
            self.assertRefused(403, "wrong secret", self.state.pair_claim, started["pair_id"], "0" * 64)
        self.assertRefused(404, "no such pairing", self.state.pair_claim, started["pair_id"], secret)

    def test_codes_expire_and_pending_pairings_are_capped(self):
        _, started = self.start()
        self.clock.advance(PAIR_TTL_SECONDS)
        self.assertRefused(404, "no such pairing", self.state.pair_confirm, started["code"])
        for _ in range(3):
            self.start()
        self.assertRefused(429, "too many pending pairings", self.start)

    def test_ten_starts_a_minute_at_most(self):
        for _ in range(10):
            # Claimed pairings leave the pending table, so only the rate limit can refuse.
            secret, started = self.start()
            self.state.pair_confirm(started["code"])
            self.state.pair_claim(started["pair_id"], secret)
        self.assertRefused(429, "too many pairing attempts", self.start)
        self.clock.advance(60)
        self.assertTrue(self.start()[1]["pair_id"])

    def test_bad_inputs(self):
        self.assertRefused(400, "bad secret_hash", self.state.pair_start, "XYZ")
        self.assertRefused(400, "bad pair_id or secret", self.state.pair_claim, "x", "not hex")


class TokenTest(StateCase):
    def token(self):
        secret = secrets.token_hex(32)
        started = self.state.pair_start(hashlib.sha256(secret.encode()).hexdigest())
        self.state.pair_confirm(started["code"])
        return self.state.pair_claim(started["pair_id"], secret)["token"]

    def test_tokens_expire(self):
        token = self.token()
        self.clock.advance(TOKEN_TTL_SECONDS)
        self.assertFalse(self.state.authenticate(token))

    def test_a_fifth_token_drops_the_oldest_and_revoke_drops_one_or_all(self):
        tokens = []
        for _ in range(MAX_TOKENS + 1):
            tokens.append(self.token())
            self.clock.advance(61)
        self.assertFalse(self.state.authenticate(tokens[0]))
        self.assertTrue(all(self.state.authenticate(t) for t in tokens[1:]))
        ids = [t["token_id"] for t in self.state.status()["tokens"]]
        self.assertEqual(len(ids), MAX_TOKENS)
        self.assertEqual(self.state.revoke(ids[0]), 1)
        self.assertFalse(self.state.authenticate(tokens[1]))
        self.assertEqual(self.state.revoke(), MAX_TOKENS - 1)
        self.assertFalse(any(self.state.authenticate(t) for t in tokens))


if __name__ == "__main__":
    unittest.main()
