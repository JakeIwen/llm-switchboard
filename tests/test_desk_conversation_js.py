"""Conversation mode on the page (static/conversation_logic.js and
static/conversation.js): runs the Node tests in tests/js/, checks that the
values the page mirrors from the hub have not drifted, and feeds the hub's
own refusals of a conversation's ticket, send, grant request and beat to the
controller, while the conversation runs and while its closing cue plays."""

import hashlib
import json
import re
import secrets
import shutil
import subprocess
import unittest
from pathlib import Path

from desk_fixtures import SESSION_A, Clock, FakeIdentity, desk_config, make_home, token_for

from llm_switchboard.desk import grants
from llm_switchboard.desk.http_server import POST_ROUTES, STATE_PATH
from llm_switchboard.desk.hub_state import HubState
from llm_switchboard.desk.locks import LockStore
from llm_switchboard.desk.model import (
    MAX_TICKETS, MAX_VOICE_TEXT_BYTES, TICKET_TTL_SECONDS, ConfirmMode, DeskError, RegistrationState, RequestState,
)
from llm_switchboard.desk.refusals import NOT_SENT, Delivery, Refusal
from llm_switchboard.desk.text_rule import MAX_TEXT_BYTES

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "llm_switchboard" / "desk" / "static"
JS_TESTS = sorted((ROOT / "tests" / "js").glob("conversation*.test.js"))
PROBE = ROOT / "tests" / "js" / "refusal_probe.js"
EXPORTS = """
const L = require(process.argv[1]);
const D = require(process.argv[2]);
const C = require(process.argv[3]);
const A = require(process.argv[4]);
process.stdout.write(JSON.stringify({
  paths: C.PATH,
  confirm: L.CONFIRM, request: L.REQUEST_STATE, registration: L.REGISTRATION, limits: L.LIMITS,
  tooLong: L.draftProblem("a".repeat(L.LIMITS.MAX_DRAFT_BYTES + 1)), refusals: D.REFUSALS,
  notSent: A.NOT_SENT, grantId: L.GRANT_ID.source,
}));
"""


def node() -> str:
    found = shutil.which("node")
    if found is None:
        raise AssertionError("node is required by the suite")
    return found


class ConversationJsTests(unittest.TestCase):
    def test_node_suites(self):
        self.assertTrue(JS_TESTS, "tests/js/conversation*.test.js is missing")
        proc = subprocess.run([node(), "--test", *map(str, JS_TESTS)], capture_output=True, text=True,
                              timeout=120, check=False, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout[-6000:] + proc.stderr[-2000:])


class ConversationMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([node(), "-e", EXPORTS, str(STATIC / "conversation_logic.js"),
                               str(STATIC / "desk_logic.js"), str(STATIC / "conversation.js"),
                               str(STATIC / "conversation_answers.js")],
                              capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.js = json.loads(proc.stdout)

    def test_vocabularies_match_the_hub(self):
        self.assertEqual(sorted(self.js["confirm"].values()), sorted(m.value for m in ConfirmMode))
        self.assertEqual(sorted(self.js["request"].values()), sorted(s.value for s in RequestState))
        self.assertEqual(sorted(self.js["registration"].values()), sorted(s.value for s in RegistrationState))

    def test_the_voice_limit_and_its_refusal_match_the_hub(self):
        self.assertEqual(self.js["limits"]["MAX_DRAFT_BYTES"], MAX_VOICE_TEXT_BYTES)
        self.assertEqual(self.js["tooLong"], grants.VOICE_TEXT_TOO_LONG)

    def test_the_page_gives_up_on_a_grant_before_the_hub_drops_it(self):
        limits = self.js["limits"]
        self.assertLess(limits["FRESH_MS"], grants.GRANT_TTL_SECONDS * 1000)
        self.assertLess(limits["BEAT_ABORT_MS"], limits["BEAT_MS"])
        self.assertLess(limits["BEAT_MS"], limits["FRESH_MS"])

    def test_the_page_accepts_exactly_the_grant_ids_the_hub_issues(self):
        self.assertEqual(self.js["grantId"], f"^[0-9a-f]{{{grants.GRANT_ID_BYTES * 2}}}$")
        self.assertTrue(re.match(self.js["grantId"], _Hub(self).grant))

    def test_the_controller_calls_routes_the_hub_serves(self):
        paths = dict(self.js["paths"])
        self.assertEqual(paths.pop("STATE"), STATE_PATH)
        for name, path in paths.items():
            self.assertIn(path, POST_ROUTES, name)

    def test_the_voice_flow_reads_not_sent_from_exactly_the_hub_s_delivery_class(self):
        self.assertEqual(sorted(self.js["notSent"]), sorted(NOT_SENT))
        for reason in (Refusal.NOT_ENROLLED, Refusal.CHANNEL_IDENTITY_UNUSABLE, Refusal.REQUEST_NOT_PENDING,
                       grants.VOICE_OFF, grants.GRANT_NOT_CURRENT, grants.BAD_CONFIRM):
            self.assertIs(reason.delivery, Delivery.NOT_SENT, reason)
        # One that can follow the channel write, and one that says nothing
        # of an earlier send with the same ticket.
        for reason in (Refusal.INTERNAL_ERROR, Refusal.TICKET_ALREADY_USED):
            self.assertIs(reason.delivery, Delivery.UNPROVEN, reason)


def _identity(pid: int) -> FakeIdentity:
    return FakeIdentity(pid, "1", pid + 1, "2", SESSION_A)


class _Hub:
    """A real HubState: SESSION_A enrolled with one registration, sends and
    voice confirm on, and this page's grant."""

    def __init__(self, case: unittest.TestCase):
        self.clock = clock = Clock()
        self.state = s = HubState(LockStore(desk_config(make_home(case)).lock_file), now=clock, elapsed=clock,
                                  token_bytes=secrets.token_bytes)
        s.set_enrolled([SESSION_A])
        s.set_sends_enabled(True)
        s.set_voice_enabled(True)
        self.generation = s.register(1, SESSION_A, _identity(100))
        self.token = token_for(s)
        self.grant = self.acquire()["grant"]

    def acquire(self, generation=None, epoch=None, token=None) -> dict:
        """The page's grant request."""
        return self.state.grant_conversation(self.token if token is None else token, SESSION_A,
                                             self.generation if generation is None else generation,
                                             self.state.epoch if epoch is None else epoch)

    def beat(self, token=None) -> dict:
        return self.state.beat_grant(self.token if token is None else token, self.grant)

    def ticket(self, generation=None, epoch=None, session_id=SESSION_A) -> str:
        return self.state.issue_ticket(session_id, self.generation if generation is None else generation,
                                       self.state.epoch if epoch is None else epoch)["ticket"]

    def reregister(self, identity=None) -> int:
        self.state.drop_registration(1)
        return self.state.register(1, SESSION_A, identity or _identity(300))

    def unusable_identity(self) -> None:
        """The target comes back with a channel identity a lock can't name,
        and this page's grant follows it."""
        self.generation = self.reregister(FakeIdentity(0, "", 301, "2", SESSION_A))
        self.grant = self.acquire()["grant"]

    def send(self, ticket, text="x", between=None, token=None, confirm=ConfirmMode.VOICE):
        """The hub's send route: begin_send, then the handoff."""
        request = self.state.begin_send(ticket, text, self.token if token is None else token, confirm, self.grant)
        if between:
            between()
        return self.state.hand_off(request.request_id)

    def pair_another_page(self) -> None:
        """A second tablet pairs while this page holds no grant."""
        self.state.end_grant(self.token, self.grant)
        secret = secrets.token_hex(32)
        started = self.state.pair_start(hashlib.sha256(secret.encode()).hexdigest())
        self.state.pair_confirm(started["code"])
        self.state.pair_claim(started["pair_id"], secret)


# A token the hub never issued.
BAD_TOKEN = "f" * 43


def _tap_pending(h: _Hub) -> None:
    h.state.begin_send(h.ticket(), "y", h.token)


def _send_after(change):
    def run(h: _Hub):
        ticket = h.ticket()
        change(h)
        h.send(ticket)
    return run


def _fill_tickets(h: _Hub) -> None:
    for _ in range(MAX_TICKETS):
        h.ticket()
    h.ticket()


def _busy_send(h: _Hub) -> None:
    ticket = h.ticket()
    _tap_pending(h)
    h.send(ticket)


def _send_twice(h: _Hub) -> None:
    ticket = h.ticket()
    h.send(ticket)
    h.send(ticket)


def _expired_ticket(h: _Hub) -> None:
    ticket = h.ticket()
    h.clock.advance(TICKET_TTL_SECONDS)
    h.send(ticket)


# (path, setup that ends in the refused call): each call a conversation
# makes, refused by the hub's own state machine.
HUB_REFUSALS = {
    "ticket, no registration": ("ticket", lambda h: (h.state.drop_registration(1), h.ticket())),
    "ticket, two registrations": ("ticket", lambda h: (h.state.register(2, SESSION_A, _identity(200)), h.ticket())),
    "ticket, stale epoch": ("ticket", lambda h: h.ticket(epoch="0" * 32)),
    "ticket, stale generation": ("ticket", lambda h: (h.reregister(), h.ticket())),
    "ticket, not enrolled": ("ticket", lambda h: (h.state.set_enrolled([]), h.ticket())),
    "ticket, busy": ("ticket", lambda h: (_tap_pending(h), h.ticket())),
    "ticket, table full": ("ticket", _fill_tickets),
    "ticket, bad session id": ("ticket", lambda h: h.ticket(session_id="x")),
    "ticket, bad generation": ("ticket", lambda h: h.ticket(generation="3")),
    "send, no registration": ("send", _send_after(lambda h: h.state.drop_registration(1))),
    "send, two registrations": ("send", _send_after(lambda h: h.state.register(2, SESSION_A, _identity(200)))),
    "send, stale epoch": ("send", lambda h: h.send("0" * 32 + ".x")),
    "send, stale generation": ("send", _send_after(lambda h: h.reregister())),
    "send, registration gone at handoff": (
        "send", lambda h: h.send(h.ticket(), between=lambda: h.state.drop_registration(1))),
    "send, second registration at handoff": (
        "send", lambda h: h.send(h.ticket(), between=lambda: h.state.register(2, SESSION_A, _identity(200)))),
    "send, grant not current": ("send", _send_after(lambda h: h.state.end_grant(h.token, h.grant))),
    "send, grant for another target": ("send", lambda h: h.send(h.ticket(generation=h.reregister()))),
    "send, not enrolled": ("send", _send_after(lambda h: h.state.set_enrolled([]))),
    "send, channel identity unusable": ("send", lambda h: (h.unusable_identity(), h.send(h.ticket()))),
    "send, busy": ("send", _busy_send),
    "send, sends off": ("send", _send_after(lambda h: h.state.set_sends_enabled(False))),
    "send, voice confirm off": ("send", _send_after(lambda h: h.state.set_voice_enabled(False))),
    "send, bad token": ("send", lambda h: h.send(h.ticket(), token=BAD_TOKEN)),
    "send, bad confirm": ("send", lambda h: h.send(h.ticket(), confirm="shout")),
    "send, bad ticket": ("send", lambda h: h.send("x")),
    "send, unknown ticket": ("send", lambda h: h.send(h.state.epoch + ".x")),
    "send, ticket expired": ("send", _expired_ticket),
    "send, ticket already used": ("send", _send_twice),
    "send, empty": ("send", lambda h: h.send(h.ticket(), text="")),
    "send, too long": ("send", lambda h: h.send(h.ticket(), text="a" * (MAX_TEXT_BYTES + 1))),
    "send, invalid character": ("send", lambda h: h.send(h.ticket(), text="a\ud800")),
    "send, outer whitespace": ("send", lambda h: h.send(h.ticket(), text=" x")),
    "send, control character": ("send", lambda h: h.send(h.ticket(), text="a\x07b")),
    "send, command character": ("send", lambda h: h.send(h.ticket(), text="/x")),
    "send, too long for a voice send": ("send", lambda h: h.send(h.ticket(), text="a" * (MAX_VOICE_TEXT_BYTES + 1))),
    "grant, stale epoch": ("grant", lambda h: h.acquire(epoch="0" * 32)),
    "grant, another page paired": ("grant", lambda h: (h.pair_another_page(), h.acquire())),
    "grant, voice confirm off": ("grant", lambda h: (h.state.set_voice_enabled(False), h.acquire())),
    "grant, sends off": ("grant", lambda h: (h.state.set_sends_enabled(False), h.acquire())),
    "grant, not enrolled": ("grant", lambda h: (h.state.set_enrolled([]), h.acquire())),
    "grant, no registration": ("grant", lambda h: (h.state.drop_registration(1), h.acquire())),
    "grant, two registrations": ("grant", lambda h: (h.state.register(2, SESSION_A, _identity(200)), h.acquire())),
    "grant, stale generation": ("grant", lambda h: (h.reregister(), h.acquire())),
    "grant, busy": ("grant", lambda h: (_tap_pending(h), h.acquire())),
    "grant, bad token": ("grant", lambda h: h.acquire(token=BAD_TOKEN)),
    "grant, bad session id": ("grant", lambda h: h.state.grant_conversation(h.token, "x", h.generation, h.state.epoch)),
    "grant, bad generation": ("grant", lambda h: h.acquire(generation="3")),
    "beat, after the grant was ended": ("beat", lambda h: (h.state.end_grant(h.token, h.grant), h.beat())),
    "beat, after the target re-registered": ("beat", lambda h: (h.reregister(), h.beat())),
    "beat, a second registration": ("beat", lambda h: (h.state.register(2, SESSION_A, _identity(200)), h.beat())),
    "beat, bad token": ("beat", lambda h: h.beat(token=BAD_TOKEN)),
}

# The refusals the cases above can't reach, by why. Every Refusal is driven
# or named here, so a new member fails
# test_every_refusal_is_driven_or_out_of_reach until it is one or the other;
# test_any_refusal_s_words_silence_every_path feeds every member's words to
# every path whatever its class.
OUT_OF_REACH = {
    # The HTTP layer's own, before or around the state machine.
    Refusal.WRONG_HOST, Refusal.WRONG_ORIGIN, Refusal.NOT_FOUND, Refusal.BAD_REQUEST, Refusal.METHOD_NOT_ALLOWED,
    Refusal.NOT_JSON, Refusal.LENGTH_REQUIRED, Refusal.BODY_TOO_LARGE, Refusal.TRUNCATED_BODY, Refusal.BAD_JSON,
    Refusal.INTERNAL_ERROR, Refusal.BAD_AFTER,
    # Need a full lock file or a request table full of outstanding requests.
    Refusal.LOCK_FILE_FULL, Refusal.TOO_MANY_OUTSTANDING,
    # Never answered to a conversation's calls: pairing, channels, locks and
    # clears, and the operator socket.
    Refusal.CONVERSATION_LIVE, Refusal.TOO_MANY_REGISTRATIONS, Refusal.BAD_LOCK_REF, Refusal.NO_LOCK,
    Refusal.LOCK_ID_MISMATCH, Refusal.DELIVERY_IN_PROGRESS, Refusal.CHANNEL_STILL_RUNNING,
    Refusal.CHANNEL_UNVERIFIABLE, Refusal.PAGE_OUT_OF_DATE, Refusal.BAD_SECRET_HASH,
    Refusal.TOO_MANY_PAIRING_ATTEMPTS, Refusal.TOO_MANY_PENDING_PAIRINGS, Refusal.BAD_CODE, Refusal.NO_SUCH_PAIRING,
    Refusal.BAD_PAIR_CLAIM, Refusal.WRONG_SECRET, Refusal.NOT_CONFIRMED, Refusal.UNKNOWN_OP, Refusal.BAD_TOKEN_ID,
    Refusal.REVOKE_IN_PROGRESS, Refusal.BAD_ENABLED,
}

PATHS = ("ticket", "send", "grant", "beat")
MODES = ("running", "closing")


def _refused(case: unittest.TestCase, names) -> list[dict]:
    """Each named entry of HUB_REFUSALS run against a fresh real hub."""
    cases = []
    for name in names:
        path, refuse = HUB_REFUSALS[name]
        with case.assertRaises(DeskError, msg=name) as caught:
            refuse(_Hub(case))
        reason = caught.exception.reason
        case.assertIsInstance(reason, Refusal, name)
        cases.append({"name": name, "path": path, "status": caught.exception.status, "reason": str(reason),
                      "not_sent": path == "ticket" or reason in NOT_SENT})
    return cases


def _probe(case: unittest.TestCase, cases) -> list[dict]:
    proc = subprocess.run([node(), str(PROBE)], input=json.dumps(cases), capture_output=True, text=True,
                          timeout=120, check=False, cwd=ROOT)
    case.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
    return json.loads(proc.stdout)


def _in(cases, mode: str) -> list[dict]:
    return [dict(c, mode=mode) for c in cases]


# Send refusals that are definitive, from checks the hub makes before the
# channel write.
LATE_SETTLED = ("send, not enrolled", "send, channel identity unusable", "send, registration gone at handoff")


class HubRefusalOnThePageTests(unittest.TestCase):
    """Each refusal the hub gives a conversation's ticket, send, grant
    request or beat, fed to the controller: none starts a closing cue, each
    cuts one that is playing and lets the grant go, and the reason says
    "not sent" exactly when the hub's refusal proves it."""

    def assert_silent_end(self, case, page):
        self.assertEqual(page["phase"], "ended")
        self.assertEqual(page["spoke"], 0)
        self.assertFalse(page["speaking"])
        self.assertFalse(page["holds"])
        # A conversation that held a grant released it; a refused grant request left none to release.
        self.assertEqual(len(page["released"]), 0 if case["path"] == "grant" else 1, page["released"])

    def assert_cut(self, page):
        self.assertTrue(page["playing"], "no closing cue was playing")
        self.assertFalse(page["speaking"], "the closing cue is still playing")
        self.assertEqual(page["spoke"], 0, "something was spoken in its place")
        self.assertFalse(page["holds"])
        self.assertEqual(len(page["released"]), 1, page["released"])

    def test_the_hub_gives_the_refusals_each_case_names(self):
        reasons = {c["name"]: c["reason"] for c in _refused(self, HUB_REFUSALS)}
        for name, reason in {
            "ticket, no registration": Refusal.NOT_REGISTERED, "ticket, two registrations": Refusal.AMBIGUOUS,
            "send, second registration at handoff": Refusal.TARGET_CHANGED_NOT_SENT,
            "send, registration gone at handoff": Refusal.REQUEST_NOT_PENDING,
            "send, not enrolled": Refusal.NOT_ENROLLED, "send, bad token": Refusal.UNAUTHORIZED,
            "send, channel identity unusable": Refusal.CHANNEL_IDENTITY_UNUSABLE,
            "grant, another page paired": Refusal.ANOTHER_PAGE, "grant, busy": Refusal.BUSY,
            "grant, no registration": Refusal.NOT_REGISTERED, "grant, bad token": Refusal.UNAUTHORIZED,
            "beat, after the grant was ended": Refusal.GRANT_NOT_CURRENT,
            "beat, after the target re-registered": Refusal.TARGET_CHANGED,
            "beat, bad token": Refusal.UNAUTHORIZED,
        }.items():
            self.assertEqual(reasons[name], reason, name)

    def test_every_refusal_is_driven_or_out_of_reach(self):
        driven = {Refusal(c["reason"]) for c in _refused(self, HUB_REFUSALS)}
        self.assertEqual(driven & OUT_OF_REACH, set())
        self.assertEqual(set(Refusal) - driven - OUT_OF_REACH, set(), "classify each new Refusal here")

    def test_each_refusal_while_the_conversation_runs_ends_it_in_silence(self):
        cases = _in(_refused(self, HUB_REFUSALS), "running")
        for case, page in zip(cases, _probe(self, cases), strict=True):
            with self.subTest(case["name"], reason=case["reason"]):
                self.assert_silent_end(case, page)
                if case["path"] in ("ticket", "send"):
                    self.assertEqual(page["notSent"], case["not_sent"], page["reason"])
                    self.assertEqual(page["unknown"], not case["not_sent"], page["reason"])

    def test_each_refusal_cuts_the_closing_cue_playing(self):
        cases = _in(_refused(self, HUB_REFUSALS), "closing")
        for case, page in zip(cases, _probe(self, cases), strict=True):
            with self.subTest(case["name"], reason=case["reason"]):
                self.assert_cut(page)
                if case["path"] == "send":
                    self.assertEqual(page["notSent"], case["not_sent"], page["reason"])

    def test_a_definitive_refusal_that_comes_after_the_end_settles_its_reason(self):
        cases = _in(_refused(self, LATE_SETTLED), "closing")
        for case, page in zip(cases, _probe(self, cases), strict=True):
            with self.subTest(case["name"], reason=case["reason"]):
                self.assertTrue(case["not_sent"])
                self.assertEqual(page["phase"], "ended")
                self.assertTrue(page["notSent"], page["reason"])

    def test_any_refusal_s_words_silence_every_path(self):
        # Whatever its class, and at the status the hub gives it or any
        # other: no member's words start a cue on any call, or leave one playing.
        statuses = {}
        for c in _refused(self, HUB_REFUSALS):
            statuses.setdefault(c["reason"], set()).add(c["status"])
        cases = [{"name": f"{path} {mode} {status} {reason}", "path": path, "mode": mode, "status": status,
                  "reason": str(reason)}
                 for reason in Refusal for status in sorted({409} | statuses.get(reason, set()))
                 for path in PATHS for mode in MODES]
        for case, page in zip(cases, _probe(self, cases), strict=True):
            with self.subTest(case["name"]):
                if case["mode"] == "closing":
                    self.assert_cut(page)
                else:
                    self.assert_silent_end(case, page)


if __name__ == "__main__":
    unittest.main()
