"""Conversation mode on the page (static/conversation_logic.js and
static/conversation.js): runs the Node tests in tests/js/, checks that the
values the page mirrors from the hub have not drifted, and feeds the hub's
own refusals of a ticket or a send to the controller."""

import json
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
    MAX_TICKETS, MAX_VOICE_TEXT_BYTES, ConfirmMode, DeskError, RegistrationState, RequestState,
)
from llm_switchboard.desk.refusals import OWNERSHIP_KEPT, Ownership, Refusal

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "llm_switchboard" / "desk" / "static"
JS_TESTS = sorted((ROOT / "tests" / "js").glob("conversation*.test.js"))
PROBE = ROOT / "tests" / "js" / "refusal_probe.js"
EXPORTS = """
const L = require(process.argv[1]);
const D = require(process.argv[2]);
const C = require(process.argv[3]);
process.stdout.write(JSON.stringify({
  paths: C.PATH,
  confirm: L.CONFIRM, request: L.REQUEST_STATE, registration: L.REGISTRATION, limits: L.LIMITS,
  tooLong: L.draftProblem("a".repeat(L.LIMITS.MAX_DRAFT_BYTES + 1)), refusals: D.REFUSALS,
  ownershipKept: L.OWNERSHIP_KEPT,
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
                               str(STATIC / "desk_logic.js"), str(STATIC / "conversation.js")],
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

    def test_the_controller_calls_routes_the_hub_serves(self):
        paths = dict(self.js["paths"])
        self.assertEqual(paths.pop("STATE"), STATE_PATH)
        for name, path in paths.items():
            self.assertIn(path, POST_ROUTES, name)

    def test_the_page_speaks_after_exactly_the_refusals_the_hub_says_keep_ownership(self):
        self.assertEqual(sorted(self.js["ownershipKept"]), sorted(OWNERSHIP_KEPT))
        self.assertTrue(OWNERSHIP_KEPT)

    def test_refusals_that_end_the_grant_or_registration_do_not_keep_ownership(self):
        for reason in (Refusal.GRANT_NOT_CURRENT, Refusal.GRANT_WRONG_TARGET, Refusal.TARGET_CHANGED,
                       Refusal.STALE_EPOCH, Refusal.STALE_GENERATION, Refusal.TARGET_CHANGED_NOT_SENT,
                       Refusal.NOT_REGISTERED, Refusal.AMBIGUOUS, Refusal.NOT_ENROLLED, Refusal.UNAUTHORIZED):
            self.assertIs(reason.ownership, Ownership.UNPROVEN, reason)

    def test_voice_send_refusals_are_definitive_on_the_page(self):
        # A send refused for these reasons was never handed off, so the page
        # may say "not sent"; any reason missing here reads as "unknown".
        for reason in (grants.VOICE_OFF, grants.GRANT_NOT_CURRENT, grants.GRANT_WRONG_TARGET,
                       grants.VOICE_TEXT_TOO_LONG, grants.BAD_CONFIRM):
            self.assertIn(reason, self.js["refusals"])


def _identity(pid: int) -> FakeIdentity:
    return FakeIdentity(pid, "1", pid + 1, "2", SESSION_A)


class _Hub:
    """A real HubState: SESSION_A enrolled with one registration, sends and
    voice confirm on, and this page's grant."""

    def __init__(self, case: unittest.TestCase):
        clock = Clock()
        self.state = s = HubState(LockStore(desk_config(make_home(case)).lock_file), now=clock, elapsed=clock,
                                  token_bytes=secrets.token_bytes)
        s.set_enrolled([SESSION_A])
        s.set_sends_enabled(True)
        s.set_voice_enabled(True)
        self.generation = s.register(1, SESSION_A, _identity(100))
        self.token = token_for(s)
        self.grant = s.grant_conversation(self.token, SESSION_A, self.generation, s.epoch)["grant"]

    def ticket(self, generation=None, epoch=None) -> str:
        return self.state.issue_ticket(SESSION_A, self.generation if generation is None else generation,
                                       self.state.epoch if epoch is None else epoch)["ticket"]

    def reregister(self) -> int:
        self.state.drop_registration(1)
        return self.state.register(1, SESSION_A, _identity(300))

    def send(self, ticket, text="x", between=None):
        """The hub's send route: begin_send, then the handoff."""
        request = self.state.begin_send(ticket, text, self.token, ConfirmMode.VOICE, self.grant)
        if between:
            between()
        return self.state.hand_off(request.request_id)


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


# (path, setup that ends in the refused call): the page's ticket and send,
# each refused by the hub's own state machine.
HUB_REFUSALS = {
    "ticket, no registration": ("ticket", lambda h: (h.state.drop_registration(1), h.ticket())),
    "ticket, two registrations": ("ticket", lambda h: (h.state.register(2, SESSION_A, _identity(200)), h.ticket())),
    "ticket, stale epoch": ("ticket", lambda h: h.ticket(epoch="0" * 32)),
    "ticket, stale generation": ("ticket", lambda h: (h.reregister(), h.ticket())),
    "ticket, not enrolled": ("ticket", lambda h: (h.state.set_enrolled([]), h.ticket())),
    "ticket, busy": ("ticket", lambda h: (_tap_pending(h), h.ticket())),
    "ticket, table full": ("ticket", _fill_tickets),
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
    "send, busy": ("send", _busy_send),
    "send, sends off": ("send", _send_after(lambda h: h.state.set_sends_enabled(False))),
    "send, voice confirm off": ("send", _send_after(lambda h: h.state.set_voice_enabled(False))),
    "send, text rule": ("send", lambda h: h.send(h.ticket(), text=" x")),
    "send, too long for a voice send": ("send", lambda h: h.send(h.ticket(), text="a" * (MAX_VOICE_TEXT_BYTES + 1))),
}


class HubRefusalOnThePageTests(unittest.TestCase):
    """Each refusal the hub gives the page's ticket or send, fed to the
    controller: a refusal that keeps ownership ends with the closing cue,
    and every other ends the conversation as lost, in silence."""

    def test_the_page_acts_on_the_hub_s_class_of_each_refusal(self):
        cases = []
        for name, (path, refuse) in HUB_REFUSALS.items():
            with self.assertRaises(DeskError, msg=name) as caught:
                refuse(_Hub(self))
            self.assertIsInstance(caught.exception.reason, Refusal, name)
            cases.append({"name": name, "path": path, "status": caught.exception.status,
                          "reason": str(caught.exception.reason), "kept": caught.exception.reason in OWNERSHIP_KEPT})
        reasons = {c["name"]: c["reason"] for c in cases}
        self.assertEqual(reasons["ticket, no registration"], Refusal.NOT_REGISTERED)
        self.assertEqual(reasons["ticket, two registrations"], Refusal.AMBIGUOUS)
        self.assertEqual(reasons["send, second registration at handoff"], Refusal.TARGET_CHANGED_NOT_SENT)
        self.assertTrue(any(c["kept"] for c in cases) and not all(c["kept"] for c in cases))
        proc = subprocess.run([node(), str(PROBE)], input=json.dumps(cases), capture_output=True, text=True,
                              timeout=60, check=False, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        for case, page in zip(cases, json.loads(proc.stdout), strict=True):
            with self.subTest(case["name"], reason=case["reason"]):
                self.assertEqual(page["phase"], "ended")
                self.assertEqual(page["lost"], not case["kept"])
                self.assertEqual(page["cue"], case["kept"])


if __name__ == "__main__":
    unittest.main()
