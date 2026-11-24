"""Conversation mode on the hub (docs/voice-desk.md, "Conversation mode"):
the two-switch settings file, the grant and its checks at beat, end,
begin_send and hand_off, the voice notice, the one-page rule and the page
build check at pairing."""

import hashlib
import json
import os
import secrets
import threading
import unittest

from desk_fixtures import (
    SESSION_A, SESSION_B, ChannelClient, Clock, FakeIdentity, HubCase, desk_config, make_home, run_desk_cli,
    token_for,
)

from llm_switchboard.desk.enrollment import DeskSettings
from llm_switchboard.desk.grants import (
    ANOTHER_PAGE, BAD_CONFIRM, CONVERSATION_LIVE, GRANT_NOT_CURRENT, GRANT_TTL_SECONDS, GRANT_WRONG_TARGET, TARGET_CHANGED,
    VOICE_OFF, VOICE_TEXT_TOO_LONG,
)
from llm_switchboard.desk.hub_state import CHANNEL_UNVERIFIABLE, HubState
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import MAX_VOICE_TEXT_BYTES, TOKEN_TTL_SECONDS, ChannelProcess, ConfirmMode, DeskError, LockStoreError
from llm_switchboard.desk.operator import OperatorOp, operator_request
from llm_switchboard.desk.page_build import PAGE_OUT_OF_DATE
from llm_switchboard.desk.protocol import DELIVERY_SUFFIX, VOICE_NOTICE, MessageType


def identity(session_id: str, pid: int = 100) -> FakeIdentity:
    return FakeIdentity(pid, "1", pid + 1, "2", session_id)


def new_token(state: HubState) -> str:
    secret = secrets.token_hex(32)
    started = state.pair_start(hashlib.sha256(secret.encode()).hexdigest())
    state.pair_confirm(started["code"])
    return state.pair_claim(started["pair_id"], secret)["token"]


class TimedLockStore(LockStore):
    """The real lock file, plus time passing during its durable write, and a
    remove that can fail or stall (an SD card) until `remove_gate` is set."""

    after_add = None
    fail_remove = False
    remove_gate: threading.Event | None = None

    def add(self, lock):
        super().add(lock)
        if self.after_add:
            self.after_add()

    def remove(self, lock_id):
        if self.remove_gate is not None:
            self.remove_gate.wait(30)
        if self.fail_remove:
            raise LockStoreError("lock file unwritable")
        super().remove(lock_id)


class SteppingClock(Clock):
    """A wall clock that can also move on by itself after a given number of
    reads: time passing inside one call, between two of its readings."""

    _step = None

    def step_after(self, reads: int, seconds: float) -> None:
        self._step = [reads, seconds]

    def __call__(self) -> float:
        value = self.now
        if self._step:
            self._step[0] -= 1
            if not self._step[0]:
                self.now += self._step[1]
                self._step = None
        return value


class GrantStateCase(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.config = desk_config(self.home)
        # The wall clock (timestamps, tokens, tickets) and the elapsed-time
        # clock the grant lease runs on; `wait` moves both, as real time does.
        self.clock = SteppingClock()
        self.elapsed = Clock(1000.0)
        self.store = TimedLockStore(self.config.lock_file)
        self.state = HubState(self.store, now=self.clock, elapsed=self.elapsed, token_bytes=secrets.token_bytes)
        self.state.set_enrollment({SESSION_A: "pi", SESSION_B: "pi"})
        self.state.set_sends_enabled(True)
        self.state.set_voice_enabled(True)
        self.token = token_for(self.state)
        self.gen_a = self.state.register(1, SESSION_A, identity(SESSION_A))
        self.gen_b = self.state.register(2, SESSION_B, identity(SESSION_B, 200))

    def wait(self, seconds):
        self.clock.advance(seconds)
        self.elapsed.advance(seconds)

    def refused(self, status, reason, call, *args, **kwargs):
        with self.assertRaises(DeskError) as caught:
            call(*args, **kwargs)
        self.assertEqual((caught.exception.status, caught.exception.reason), (status, reason))

    def grant(self, session_id=SESSION_A, generation=None, token=None):
        generation = self.gen_a if generation is None else generation
        return self.state.grant_conversation(token or self.token, session_id, generation, self.state.epoch)["grant"]

    def ticket(self, session_id=SESSION_A, generation=None):
        generation = self.gen_a if generation is None else generation
        return self.state.issue_ticket(session_id, generation, self.state.epoch)["ticket"]

    def voice_send(self, grant, text="hello there", session_id=SESSION_A):
        generation = self.gen_a if session_id == SESSION_A else self.gen_b
        return self.state.begin_send(self.ticket(session_id, generation), text, self.token, "voice", grant)


class GrantTest(GrantStateCase):
    def test_a_grant_needs_the_voice_gate_sends_and_a_free_current_target(self):
        self.state.set_voice_enabled(False)
        self.refused(403, VOICE_OFF, self.grant)
        self.state.set_voice_enabled(True)
        self.state.set_sends_enabled(False)
        self.refused(403, "sends off", self.grant)
        self.state.set_sends_enabled(True)
        self.refused(409, "stale generation", self.grant, generation=self.gen_a + 10)
        self.refused(409, "stale epoch", self.state.grant_conversation, self.token, SESSION_A, self.gen_a, "0" * 32)
        self.refused(401, "unauthorized", self.state.grant_conversation, "nope", SESSION_A, self.gen_a,
                     self.state.epoch)
        self.assertEqual(len(self.grant()), 32)

    def test_a_grant_is_refused_while_a_second_token_is_paired(self):
        second = new_token(self.state)
        self.refused(409, ANOTHER_PAGE, self.grant)
        self.refused(409, ANOTHER_PAGE, self.grant, token=second)
        self.state.revoke(self.state.status()["tokens"][0]["token_id"])
        self.assertTrue(self.grant(token=second))

    def test_a_token_left_out_of_the_one_page_rule_stays_gone_when_the_wall_clock_steps_back(self):
        self.wait(60)
        second = new_token(self.state)
        # The first token has just expired and housekeeping hasn't run.
        self.clock.advance(TOKEN_TTL_SECONDS - 60 + 1)
        grant = self.grant(token=second)
        self.clock.advance(-2)
        self.assertFalse(self.state.authenticate(self.token))
        self.assertEqual(len(self.state.status()["tokens"]), 1)
        self.refused(401, "unauthorized", self.state.beat_grant, self.token, grant)
        self.assertEqual(self.state.beat_grant(second, grant)["grant"], grant)

    def test_a_requester_whose_token_expires_during_its_grant_request_is_refused(self):
        self.wait(60)
        second = new_token(self.state)
        # One second left on the first token when its request is authorized;
        # expired by the clock's next reading, when tokens are pruned.
        self.clock.advance(TOKEN_TTL_SECONDS - 60 - 1)
        self.clock.step_after(1, 2)
        self.refused(401, "unauthorized", self.grant)
        self.assertEqual(len(self.state.status()["tokens"]), 1)
        grant = self.grant(token=second)
        self.assertEqual(self.state.beat_grant(second, grant)["grant"], grant)

    def test_beats_renew_and_an_expired_grant_is_never_revived(self):
        grant = self.grant()
        for _ in range(3):
            self.wait(GRANT_TTL_SECONDS - 1)
            self.assertEqual(self.state.beat_grant(self.token, grant)["grant"], grant)
        self.wait(GRANT_TTL_SECONDS)
        self.refused(409, GRANT_NOT_CURRENT, self.state.beat_grant, self.token, grant)
        self.refused(409, GRANT_NOT_CURRENT, self.state.end_grant, self.token, grant)

    def test_the_lease_runs_on_elapsed_time_whatever_the_wall_clock_does(self):
        grant = self.grant()
        # A wall-clock correction backwards must not extend the 15 s lease ...
        self.clock.advance(-3600)
        self.wait(GRANT_TTL_SECONDS)
        self.refused(409, GRANT_NOT_CURRENT, self.state.beat_grant, self.token, grant)
        # ... and one forwards must not cut a live grant short.
        grant = self.grant()
        self.clock.advance(600)
        self.wait(GRANT_TTL_SECONDS - 1)
        self.assertEqual(self.state.beat_grant(self.token, grant)["grant"], grant)

    def test_a_delayed_end_naming_an_old_grant_leaves_the_newer_one(self):
        old = self.grant()
        new = self.grant()
        self.assertNotEqual(old, new)
        self.refused(409, GRANT_NOT_CURRENT, self.state.end_grant, self.token, old)
        self.refused(409, GRANT_NOT_CURRENT, self.state.beat_grant, self.token, old)
        self.assertEqual(self.state.beat_grant(self.token, new)["grant"], new)
        self.assertEqual(self.state.end_grant(self.token, new), {"ended": True})
        self.refused(409, GRANT_NOT_CURRENT, self.state.beat_grant, self.token, new)

    def test_a_grant_id_not_in_the_hub_s_shape_is_refused_as_not_current(self):
        grant = self.grant()
        for bad in ("\u00e9" * 32, "\u00ff" + grant[1:], grant[:-1], grant + "0", grant.upper(), "", 7, True,
                    None, [grant], {"grant": grant}):
            with self.subTest(bad=bad):
                self.refused(409, GRANT_NOT_CURRENT, self.state.beat_grant, self.token, bad)
                self.refused(409, GRANT_NOT_CURRENT, self.state.end_grant, self.token, bad)
                self.refused(409, GRANT_NOT_CURRENT, self.voice_send, bad)
        self.assertEqual(self.state.beat_grant(self.token, grant)["grant"], grant)

    def test_a_grant_request_with_hostile_fields_is_a_bad_request(self):
        for session_id, generation, epoch in (
                ("\u00e9" * 36, self.gen_a, self.state.epoch), (None, self.gen_a, self.state.epoch),
                ([SESSION_A], self.gen_a, self.state.epoch), (SESSION_A + "\n", self.gen_a, self.state.epoch),
                (SESSION_A, None, self.state.epoch), (SESSION_A, True, self.state.epoch),
                (SESSION_A, float(self.gen_a), self.state.epoch), (SESSION_A, str(self.gen_a), self.state.epoch),
                (SESSION_A, self.gen_a, None), (SESSION_A, self.gen_a, 0)):
            with self.subTest(session_id=session_id, generation=generation, epoch=epoch):
                with self.assertRaises(DeskError) as caught:
                    self.state.grant_conversation(self.token, session_id, generation, epoch)
                self.assertEqual(caught.exception.status, 400)
        self.refused(409, "stale epoch", self.grant_with_epoch, "\u00e9" * 32)
        self.refused(409, "stale generation", self.grant, generation=10 ** 40)

    def grant_with_epoch(self, epoch):
        return self.state.grant_conversation(self.token, SESSION_A, self.gen_a, epoch)

    def test_a_beat_after_the_target_reconnected_ends_the_grant(self):
        grant = self.grant()
        self.state.drop_registration(1)
        self.state.register(3, SESSION_A, identity(SESSION_A, 300))
        self.refused(409, TARGET_CHANGED, self.state.beat_grant, self.token, grant)
        self.refused(409, GRANT_NOT_CURRENT, self.state.beat_grant, self.token, grant)

    def test_revoke_and_token_expiry_end_the_grant(self):
        grant = self.grant()
        self.state.revoke()
        self.refused(401, "unauthorized", self.state.beat_grant, self.token, grant)
        self.assertIsNone(self.state.status()["conversation"])

    def test_pairing_claims_are_refused_while_a_grant_is_live(self):
        grant = self.grant()
        status = self.state.status()["conversation"]
        self.assertEqual((status["session_id"], status["generation"]), (SESSION_A, self.gen_a))
        secret = secrets.token_hex(32)
        started = self.state.pair_start(hashlib.sha256(secret.encode()).hexdigest())
        self.state.pair_confirm(started["code"])
        self.refused(409, CONVERSATION_LIVE, self.state.pair_claim, started["pair_id"], secret)
        self.state.end_grant(self.token, grant)
        # The pairing is still waiting and can be claimed once the conversation ended.
        self.assertIn("token", self.state.pair_claim(started["pair_id"], secret))

    def test_an_expired_grant_no_longer_blocks_pairing(self):
        self.grant()
        self.wait(GRANT_TTL_SECONDS)
        self.assertTrue(new_token(self.state))


class VoiceSendTest(GrantStateCase):
    def test_an_omitted_confirm_is_a_tap_send_as_before(self):
        self.state.set_voice_enabled(False)
        request = self.state.begin_send(self.ticket(), "hello there", self.token)
        self.assertIs(request.confirm, ConfirmMode.TAP)
        _, content = self.state.hand_off(request.request_id)
        self.assertEqual(content, "hello there" + DELIVERY_SUFFIX.format(request_id=request.request_id))

    def test_a_supplied_confirm_other_than_tap_or_voice_is_refused_even_null(self):
        ticket = self.ticket(SESSION_B, self.gen_b)
        for confirm in (None, 1, "", "shout", "TAP", True, ["tap"]):
            with self.subTest(confirm=confirm):
                self.refused(400, BAD_CONFIRM, self.state.begin_send, ticket, "hi", self.token, confirm)
        # None of them consumed the ticket.
        self.assertIs(self.state.begin_send(ticket, "hi", self.token, "tap").confirm, ConfirmMode.TAP)

    def test_a_voice_send_carries_the_notice_after_the_delivery_suffix(self):
        request = self.voice_send(self.grant())
        self.assertIs(request.confirm, ConfirmMode.VOICE)
        _, content = self.state.hand_off(request.request_id)
        self.assertEqual(content, "hello there" + DELIVERY_SUFFIX.format(request_id=request.request_id)
                         + VOICE_NOTICE)
        self.assertTrue(VOICE_NOTICE.strip().startswith("[voice desk:") and VOICE_NOTICE.endswith("]"))

    def test_a_voice_send_needs_the_gate_the_current_grant_for_its_target_and_short_text(self):
        grant = self.grant()
        self.refused(409, GRANT_NOT_CURRENT, self.voice_send, None)
        self.refused(409, GRANT_NOT_CURRENT, self.voice_send, "f" * 32)
        self.refused(409, GRANT_WRONG_TARGET, self.voice_send, grant, session_id=SESSION_B)
        self.refused(400, VOICE_TEXT_TOO_LONG, self.voice_send, grant, "x" * (MAX_VOICE_TEXT_BYTES + 1))
        self.state.set_voice_enabled(False)
        self.refused(403, VOICE_OFF, self.voice_send, grant)
        # None of these consumed a ticket or left the target busy.
        self.state.set_voice_enabled(True)
        self.assertEqual(self.voice_send(grant, "x" * MAX_VOICE_TEXT_BYTES).state.value, "pending")

    def assert_handoff_refused(self, status, reason, request):
        self.refused(status, reason, self.state.hand_off, request.request_id)
        self.assertEqual(self.state.request_state(request.request_id), "expired")
        self.assertIsNone(self.target_lock())

    def target_lock(self):
        return next(t for t in self.state.snapshot()["targets"] if t["session_id"] == SESSION_A)["lock"]

    def test_voice_switched_off_between_begin_send_and_hand_off_refuses_it(self):
        request = self.voice_send(self.grant())
        self.state.set_voice_enabled(False)
        self.assert_handoff_refused(403, VOICE_OFF, request)

    def test_a_grant_replaced_between_begin_send_and_hand_off_refuses_it(self):
        request = self.voice_send(self.grant())
        # Its own target is busy now, so the new grant is for the other one.
        self.grant(SESSION_B, self.gen_b)
        self.assert_handoff_refused(409, GRANT_NOT_CURRENT, request)

    def test_a_grant_expired_or_ended_between_begin_send_and_hand_off_refuses_it(self):
        grant = self.grant()
        request = self.voice_send(grant)
        self.wait(GRANT_TTL_SECONDS)
        self.assert_handoff_refused(409, GRANT_NOT_CURRENT, request)
        grant = self.grant()
        request = self.voice_send(grant)
        self.state.end_grant(self.token, grant)
        self.assert_handoff_refused(409, GRANT_NOT_CURRENT, request)

    def promptly(self, call, *args):
        """Run `call` on another thread and fail unless it returns within
        5 s: a call stuck behind the state's lock would wait for good."""
        outcome = {}

        def run():
            try:
                outcome["value"] = call(*args)
            except BaseException as exc:  # handed back to the test thread
                outcome["error"] = exc
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), f"{call.__name__} was held up")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    def no_process_check(self, pid, start):
        self.fail("a lock whose event was never written needs no channel-process check")

    def refuse_after_the_lock_write(self, request):
        """Hand off `request` while its grant expires during the durable lock
        write (a slow SD card); returns the lock the target is left with."""
        self.store.after_add = lambda: self.wait(GRANT_TTL_SECONDS)
        self.refused(409, GRANT_NOT_CURRENT, self.promptly, self.state.hand_off, request.request_id)
        self.store.after_add = None
        self.assertEqual(self.state.request_state(request.request_id), "expired")
        lock = self.target_lock()
        self.assertEqual(lock["request_id"], request.request_id)
        return lock

    def test_a_grant_that_expires_during_the_durable_lock_write_refuses_the_handoff_and_keeps_the_target_blocked(self):
        spare = self.ticket()  # issued while the target was still free
        request = self.voice_send(self.grant())
        self.wait(GRANT_TTL_SECONDS - 1)
        self.store.after_add = lambda: self.wait(1)  # the write crosses expiry by a moment
        self.refused(409, GRANT_NOT_CURRENT, self.state.hand_off, request.request_id)
        self.store.after_add = None
        self.assertEqual(self.state.request_state(request.request_id), "expired")
        # The lock just written stays, in memory and on disk, and keeps the
        # target busy; the store itself is fine.
        lock = self.target_lock()
        self.assertEqual((lock["request_id"], lock["earlier_run"]), (request.request_id, False))
        self.assertEqual([saved.lock_id for saved in LockStore(self.config.lock_file).load()], [lock["lock_id"]])
        self.assertIsNone(self.state.snapshot()["lock_store_error"])
        self.refused(409, "busy", self.state.issue_ticket, SESSION_A, self.gen_a, self.state.epoch)
        self.refused(409, "busy", self.state.begin_send, spare, "hello there", self.token)
        # Housekeeping keeps the evidence; nothing reached the channel, so
        # Clear releases it with no channel-process check.
        self.state.expire()
        self.state.clear_lock(SESSION_A, lock["lock_id"], self.no_process_check)
        self.assertIsNone(self.target_lock())
        self.assertEqual(LockStore(self.config.lock_file).load(), [])
        tap = self.state.begin_send(self.ticket(), "hello there", self.token)
        self.assertEqual(self.state.hand_off(tap.request_id)[0], 1)

    def test_that_refusal_writes_nothing_more_while_holding_the_state_s_lock(self):
        # Every removal stalls until the test lets it go: the refused handoff
        # returns anyway, and reads, a beat and a revoke go straight through.
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.store.remove_gate = gate
        lock = self.refuse_after_the_lock_write(self.voice_send(self.grant()))
        self.promptly(self.state.snapshot)
        self.promptly(self.state.status)
        other = self.promptly(self.grant, SESSION_B, self.gen_b)
        self.assertEqual(self.promptly(self.state.beat_grant, self.token, other)["grant"], other)
        self.assertEqual(self.target_lock(), lock)
        token_id = self.state.status()["tokens"][0]["token_id"]
        self.assertEqual(self.promptly(self.state.revoke, token_id).count, 1)
        # The target stayed blocked throughout, and the lock is still released
        # with no channel-process check.
        gate.set()
        self.state.clear_lock(SESSION_A, lock["lock_id"], self.no_process_check)
        self.assertIsNone(self.target_lock())

    def test_a_failed_release_of_that_lock_fails_closed_like_any_clear(self):
        lock = self.refuse_after_the_lock_write(self.voice_send(self.grant()))
        self.store.fail_remove = True
        self.refused(503, "lock file unwritable", self.state.clear_lock, SESSION_A, lock["lock_id"],
                     self.no_process_check)
        # The lock stays, the target busy, and the store refuses every send.
        self.assertEqual(self.target_lock(), lock)
        self.assertEqual(self.state.snapshot()["lock_store_error"], "lock file unwritable")
        self.refused(503, "lock file unwritable", self.state.issue_ticket, SESSION_B, self.gen_b, self.state.epoch)
        # Still provably never written, so a later clear needs no check either.
        self.store.fail_remove = False
        self.state.clear_lock(SESSION_A, lock["lock_id"], self.no_process_check)
        self.assertIsNone(self.target_lock())

    def test_after_a_hub_restart_that_lock_is_an_earlier_run_s_and_gets_the_process_check(self):
        lock = self.refuse_after_the_lock_write(self.voice_send(self.grant()))
        restarted = HubState(LockStore(self.config.lock_file), now=self.clock, elapsed=self.elapsed,
                             token_bytes=secrets.token_bytes)
        self.assertEqual([(saved["lock_id"], saved["earlier_run"]) for saved in restarted.status()["locks"]],
                         [(lock["lock_id"], True)])
        self.refused(503, CHANNEL_UNVERIFIABLE, restarted.clear_lock, SESSION_A, lock["lock_id"])
        restarted.clear_lock(SESSION_A, lock["lock_id"], lambda pid, start: ChannelProcess.GONE)
        self.assertEqual(LockStore(self.config.lock_file).load(), [])

    def test_the_snapshot_lists_how_each_request_was_confirmed(self):
        # The page never voices a reply to a voice send itself, also after
        # its conversation ended or when the send's answer never reached it.
        voice = self.voice_send(self.grant())
        tap = self.state.begin_send(self.ticket(SESSION_B, self.gen_b), "hello there", self.token)
        confirms = {r["request_id"]: r["confirm"] for r in self.state.snapshot()["requests"]}
        self.assertEqual(confirms, {voice.request_id: "voice", tap.request_id: "tap"})

    def test_a_tap_send_does_not_depend_on_the_grant_or_the_gate(self):
        request = self.state.begin_send(self.ticket(), "hello there", self.token, "tap")
        self.state.set_voice_enabled(False)
        self.assertEqual(self.state.hand_off(request.request_id)[0], 1)


class SettingsFileTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.config = desk_config(self.home)
        self.config.config_dir.mkdir(mode=0o700, parents=True)
        self.settings = DeskSettings(self.config.settings_file)

    def content(self):
        return json.loads(self.config.settings_file.read_text())

    def test_each_switch_keeps_the_other_key_even_from_a_file_with_only_the_old_key(self):
        self.config.settings_file.write_text('{"sends_enabled": true}')
        self.settings.set_voice_confirm_enabled(True)
        self.assertEqual(self.content(), {"sends_enabled": True, "voice_confirm_enabled": True})
        self.settings.set_sends_enabled(False)
        self.assertEqual(self.content(), {"sends_enabled": False, "voice_confirm_enabled": True})
        self.assertTrue(self.settings.voice_confirm_enabled())
        self.assertFalse(self.settings.sends_enabled())

    def test_the_file_stays_two_boolean_keys_and_a_broken_one_reads_and_rewrites_as_off(self):
        self.config.settings_file.write_text('{"sends_enabled": true, "voice_confirm_enabled": "yes", "x": 1}')
        self.assertFalse(self.settings.voice_confirm_enabled())
        self.settings.set_sends_enabled(True)
        self.assertEqual(self.content(), {"sends_enabled": True})
        self.config.settings_file.write_text("{broken")
        with self.assertLogs("llm_switchboard.desk", "WARNING"):
            self.assertFalse(self.settings.voice_confirm_enabled())
        with self.assertLogs("llm_switchboard.desk", "WARNING"):
            self.settings.set_voice_confirm_enabled(True)
        self.assertEqual(self.content(), {"voice_confirm_enabled": True})

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_the_voice_gate_is_read_fresh_so_an_unreadable_file_means_off(self):
        self.settings.set_voice_confirm_enabled(True)
        self.settings.set_sends_enabled(True)
        self.assertTrue(self.settings.voice_confirm_enabled())
        self.assertTrue(self.settings.sends_enabled())  # now cached
        os.chmod(self.config.settings_file, 0)
        self.addCleanup(os.chmod, self.config.settings_file, 0o600)
        with self.assertLogs("llm_switchboard.desk", "WARNING"):
            self.assertFalse(self.settings.voice_confirm_enabled())

    def test_the_offline_cli_keeps_the_other_key(self):
        self.config.settings_file.write_text('{"sends_enabled": true}')
        result = run_desk_cli(self.home, "voice", "on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("voice on (saved", result.stdout)
        self.assertEqual(self.content(), {"sends_enabled": True, "voice_confirm_enabled": True})
        self.assertEqual(run_desk_cli(self.home, "sends", "off").returncode, 0)
        self.assertEqual(self.content(), {"sends_enabled": False, "voice_confirm_enabled": True})


class ConversationHubTest(HubCase):
    """Over HTTPS and the operator socket against a running hub."""

    def voice(self, enabled: bool) -> dict:
        return operator_request(self.config.operator_socket, OperatorOp.VOICE, enabled=enabled)

    def registered(self, session_id=SESSION_A):
        client = ChannelClient(self.config.channel_socket, session_id)
        self.addCleanup(client.close)
        kind, welcome = client.first
        self.assertIs(kind, MessageType.WELCOME)
        return client, welcome["generation"]

    def post(self, path, body, token):
        status, _, data = self.call("POST", path, body, token=token)
        return status, data

    def grant(self, token, generation):
        return self.post("/desk/api/conversation", {"session_id": SESSION_A, "generation": generation,
                                                    "epoch": self.hub.epoch}, token)

    def ticket(self, token, generation):
        status, data = self.post("/desk/api/ticket", {"session_id": SESSION_A, "generation": generation,
                                                      "epoch": self.hub.epoch}, token)
        self.assertEqual(status, 200, data)
        return data["ticket"]

    def test_desk_voice_switches_the_snapshot_and_keeps_sends(self):
        token = self.pair()
        self.assertEqual(self.voice(True), {"ok": True, "voice_confirm_enabled": True})
        self.assertTrue(self.call("GET", "/desk/api/state", token=token)[2]["voice_confirm_enabled"])
        self.assertEqual(json.loads(self.config.settings_file.read_text()),
                         {"sends_enabled": True, "voice_confirm_enabled": True})
        self.assertTrue(operator_request(self.config.operator_socket, OperatorOp.STATUS)["voice_confirm_enabled"])
        self.assertEqual(self.voice(False)["voice_confirm_enabled"], False)
        self.assertFalse(self.call("GET", "/desk/api/state", token=token)[2]["voice_confirm_enabled"])
        self.assertFalse(self.voice("yes")["ok"])

    def test_a_grant_is_refused_with_the_gate_off_or_a_second_page_paired(self):
        token = self.pair()
        _, generation = self.registered()
        self.assertEqual(self.grant(token, generation), (403, {"error": VOICE_OFF}))
        self.voice(True)
        second = self.pair()
        self.assertEqual(self.grant(token, generation), (409, {"error": ANOTHER_PAGE}))
        status = operator_request(self.config.operator_socket, OperatorOp.STATUS)
        newest = status["tokens"][-1]["token_id"]
        operator_request(self.config.operator_socket, OperatorOp.REVOKE, token_id=newest)
        self.assertEqual(self.call("GET", "/desk/api/state", token=second)[0], 401)
        status, granted = self.grant(token, generation)
        self.assertEqual(status, 200, granted)
        self.assertEqual(granted["expires_in"], GRANT_TTL_SECONDS)
        self.assertEqual(self.post("/desk/api/conversation/beat", {"grant": granted["grant"]}, token)[0], 200)
        self.assertEqual(self.post("/desk/api/conversation/beat", {"grant": "\u00e9" * 32}, token),
                         (409, {"error": GRANT_NOT_CURRENT}))
        self.assertEqual(self.post("/desk/api/conversation/end", {"grant": granted["grant"]}, token),
                         (200, {"ended": True}))
        self.assertEqual(self.post("/desk/api/conversation/end", {"grant": granted["grant"]}, token),
                         (409, {"error": GRANT_NOT_CURRENT}))

    def test_a_voice_send_hands_off_with_the_notice_and_a_stale_or_missing_grant_is_refused(self):
        token = self.pair()
        channel, generation = self.registered()
        self.voice(True)
        _, granted = self.grant(token, generation)
        send = {"ticket": self.ticket(token, generation), "text": "list the files", "confirm": "voice"}
        self.assertEqual(self.post("/desk/api/send", send, token), (409, {"error": GRANT_NOT_CURRENT}))
        self.assertEqual(self.post("/desk/api/send", {**send, "grant": "0" * 32}, token),
                         (409, {"error": GRANT_NOT_CURRENT}))
        status, sent = self.post("/desk/api/send", {**send, "grant": granted["grant"]}, token)
        self.assertEqual((status, sent["state"]), (200, "handed_off"))
        kind, event = channel.read()
        self.assertIs(kind, MessageType.EVENT)
        self.assertTrue(event["content"].startswith("list the files" + DELIVERY_SUFFIX.format(
            request_id=sent["request_id"])))
        self.assertTrue(event["content"].endswith(VOICE_NOTICE))

    def test_a_tap_send_with_confirm_omitted_is_unchanged(self):
        token = self.pair()
        channel, generation = self.registered()
        status, sent = self.post("/desk/api/send", {"ticket": self.ticket(token, generation), "text": "hi"}, token)
        self.assertEqual((status, sent["state"]), (200, "handed_off"))
        self.assertEqual(channel.read()[1]["content"], "hi" + DELIVERY_SUFFIX.format(request_id=sent["request_id"]))

    def test_a_send_whose_confirm_is_null_or_not_tap_or_voice_is_a_bad_request(self):
        token = self.pair()
        channel, generation = self.registered()
        send = {"ticket": self.ticket(token, generation), "text": "hi"}
        for confirm in (None, 0, "", "shout"):
            with self.subTest(confirm=confirm):
                self.assertEqual(self.post("/desk/api/send", {**send, "confirm": confirm}, token),
                                 (400, {"error": BAD_CONFIRM}))
        self.assertIsNone(self.target(SESSION_A)["lock"])
        status, sent = self.post("/desk/api/send", send, token)
        self.assertEqual((status, sent["state"]), (200, "handed_off"))
        self.assertEqual(channel.read()[1]["content"], "hi" + DELIVERY_SUFFIX.format(request_id=sent["request_id"]))

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_the_handoff_reads_the_voice_gate_fresh_from_the_file(self):
        token = self.pair()
        channel, generation = self.registered()
        self.voice(True)
        _, granted = self.grant(token, generation)
        send = {"ticket": self.ticket(token, generation), "text": "hi", "confirm": "voice", "grant": granted["grant"]}
        # A permission change keeps the cached sends value but must read as voice off.
        self.hub.refresh_files()
        os.chmod(self.config.settings_file, 0)
        self.addCleanup(os.chmod, self.config.settings_file, 0o600)
        with self.assertLogs("llm_switchboard.desk", "WARNING"):
            self.assertEqual(self.post("/desk/api/send", send, token), (403, {"error": VOICE_OFF}))
        self.assertIsNone(self.target(SESSION_A)["lock"])

    def test_a_pairing_claim_during_a_grant_is_refused(self):
        token = self.pair()
        _, generation = self.registered()
        self.voice(True)
        _, granted = self.grant(token, generation)
        secret = secrets.token_hex(32)
        _, _, started = self.call("POST", "/desk/pair/start", {"secret_hash": hashlib.sha256(secret.encode()).hexdigest()})
        self.hub.handle_operator({"op": "pair", "code": started["code"]})
        claim = {"pair_id": started["pair_id"], "secret": secret, "build": self.hub.http.page_build}
        self.assertEqual(self.call("POST", "/desk/pair/claim", claim)[::2], (409, {"error": CONVERSATION_LIVE}))
        self.post("/desk/api/conversation/end", {"grant": granted["grant"]}, token)
        self.assertEqual(self.call("POST", "/desk/pair/claim", claim)[0], 200)


class PageBuildTest(HubCase):
    def test_the_page_carries_the_hub_s_build_and_a_claim_from_another_build_is_refused(self):
        build = self.hub.http.page_build
        self.assertRegex(build, r"^[0-9a-f]{16}$")
        status, _, page = self.call("GET", "/desk")
        self.assertEqual(status, 200)
        self.assertIn(f'<meta name="desk-build" content="{build}">'.encode(), page)
        self.assertNotIn(b"@DESK_BUILD@", page)
        secret = secrets.token_hex(32)
        _, _, started = self.call("POST", "/desk/pair/start", {"secret_hash": hashlib.sha256(secret.encode()).hexdigest()})
        self.hub.handle_operator({"op": "pair", "code": started["code"]})
        for stale in ({}, {"build": "0" * 16}, {"build": None}):
            with self.subTest(stale=stale):
                claim = {"pair_id": started["pair_id"], "secret": secret, **stale}
                self.assertEqual(self.call("POST", "/desk/pair/claim", claim)[::2], (403, {"error": PAGE_OUT_OF_DATE}))
        claim = {"pair_id": started["pair_id"], "secret": secret, "build": build}
        self.assertEqual(self.call("POST", "/desk/pair/claim", claim)[0], 200)

    def test_the_build_changes_with_any_page_file_and_the_hub_serves_what_it_built(self):
        from llm_switchboard.desk.page_build import PageFiles
        root = make_home(self)
        (root / "index.html").write_bytes(b'<meta name="desk-build" content="@DESK_BUILD@">')
        (root / "a.js").write_bytes(b"one")
        routes = {"/desk": (root / "index.html", "text/html"), "/desk/a.js": (root / "a.js", "text/javascript"),
                  "/desk/gone.js": (root / "gone.js", "text/javascript")}
        first = PageFiles(routes)
        (root / "a.js").write_bytes(b"two")
        second = PageFiles(routes)
        self.assertNotEqual(first.build, second.build)
        self.assertEqual(first.get("/desk/a.js"), (b"one", "text/javascript"))
        self.assertIsNone(first.get("/desk/gone.js"))
        self.assertEqual(first.get("/desk")[0], f'<meta name="desk-build" content="{first.build}">'.encode())

    def test_every_served_file_feeds_the_build(self):
        from llm_switchboard.desk.http_server import STATIC_ROUTES
        from llm_switchboard.desk.page_build import PageFiles
        root = make_home(self)
        routes = {}
        for route, (file, content_type) in STATIC_ROUTES.items():
            copy = root / route.strip("/").replace("/", "_")
            copy.write_bytes(file.read_bytes())
            routes[route] = (copy, content_type)
        base = PageFiles(routes).build
        self.assertIn("/desk/conversation.js", routes)
        for route, (copy, _) in routes.items():
            with self.subTest(route=route):
                original = copy.read_bytes()
                copy.write_bytes(original + b"\n")
                self.assertNotEqual(PageFiles(routes).build, base)
                copy.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
