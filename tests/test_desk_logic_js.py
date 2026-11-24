"""Decision helpers of the tablet page (static/desk_logic.js), run through node."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

LOGIC = Path(__file__).resolve().parents[1] / "llm_switchboard" / "desk" / "static" / "desk_logic.js"
SCRIPT = """
const L = require(process.argv[1]);
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const ids = new Set(input.ids);
L.pruneIds(ids, input.requests);
process.stdout.write(JSON.stringify({
  sends: input.sends.map(L.classifySend),
  frozen: L.freezeClear(input.target),
  valid: input.valid.map(([f, t]) => L.clearStillValid(f, t)),
  describe: L.describeRequestState(input.state),
  ids: Array.from(ids).sort(),
  over: (input.over || []).map(([id, snap, v, p, q]) => L.waitingOver(id, snap, v, p, q)),
}));
"""

A = {"host": "pi", "session_id": "s1", "lock": {"lock_id": "L1", "request_id": "r1"}}
B = {"host": "pi", "session_id": "s1", "lock": {"lock_id": "L2", "request_id": "r2"}}
FREE = {"host": "pi", "session_id": "s1", "lock": None}
# A's session id with the same lock id, now on a Mac.
MOVED = dict(A, host="mac")

# The selection, history and speech filters, over one snapshot.
HOSTS_SCRIPT = """
const L = require(process.argv[1]);
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const snap = input.snapshot;
process.stdout.write(JSON.stringify({
  keys: snap.targets.map(L.keyOf),
  found: input.keys.map((k) => L.findTarget(snap, k)),
  history: input.keys.map((k) => L.requestsFor(snap.requests, k).map((rq) => rq.request_id)),
  same: input.pairs.map(([a, b]) => L.sameTarget(a, b)),
  badges: input.badges.map(L.badgeFor),
}));
"""


class DeskLogicJsTests(unittest.TestCase):
    def run_node(self, **kw):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required by the suite")
        payload = {"sends": [], "target": None, "valid": [], "state": "pending", "ids": [], "requests": [], "over": []}
        payload.update(kw)
        proc = subprocess.run([node, "-e", SCRIPT, str(LOGIC)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_send_classification(self):
        sends = [
            None,                                                                   # timeout / network
            {"status": 200, "data": {"request_id": "r", "state": "handed_off"}},
            {"status": 200, "data": {"request_id": "r", "state": "unknown"}},       # hub's write failed
            {"status": 200, "data": {"request_id": "r", "state": "answered"}},
            {"status": 200, "data": None},                                          # malformed body
            {"status": 409, "data": {"error": "busy"}},
            {"status": 403, "data": {"error": "sends off"}},
            {"status": 409, "data": {"error": "target changed; not sent"}},
            {"status": 400, "data": {"error": "control character"}},
            {"status": 503, "data": {"error": "lock store error"}},
            {"status": 500, "data": None},
            {"status": 409, "data": {"error": "something new"}},                    # unrecognised 4xx
            {"status": 502, "data": {"error": "busy"}},                             # a 5xx never counts as refusal
            {"status": 409, "data": {"error": "ticket already used"}},              # first attempt may have gone through
        ]
        got = [g["outcome"] for g in self.run_node(sends=sends)["sends"]]
        self.assertEqual(got, ["unknown", "handed_off", "unknown", "answered", "unknown", "not_sent",
                               "not_sent", "not_sent", "not_sent", "unknown", "unknown", "unknown", "unknown", "ticket_used"])

    def test_wording(self):
        out = self.run_node(sends=[{"status": 200, "data": {"request_id": "r", "state": "handed_off"}}, None])
        self.assertEqual(out["sends"][0]["text"], "Handed off; waiting for the reply.")
        self.assertTrue(out["sends"][1]["text"].startswith("Delivery unknown"))
        self.assertEqual(self.run_node(state="unknown")["describe"], "Delivery unknown")
        self.assertEqual(self.run_node(state="handed_off")["describe"], "Handed off; waiting for the reply")
        ticket = self.run_node(sends=[{"status": 409, "data": {"error": "ticket already used"}}])["sends"][0]
        self.assertEqual(ticket["text"], "Ticket already used; check the original request.")
        self.assertNotIn("Not sent", ticket["text"])
        every = self.run_node(sends=[None, {"status": 200, "data": None}], state="unknown")
        for text in [s["text"] for s in every["sends"]] + [every["describe"]]:
            self.assertNotIn("delivered", text.lower())

    def test_clear_confirmation_is_frozen_and_dismissed_on_change(self):
        frozen = {"host": "pi", "session_id": "s1", "lock_id": "L1"}
        out = self.run_node(target=A, valid=[[frozen, A], [frozen, B], [frozen, FREE], [frozen, None], [None, A],
                                             [frozen, MOVED]])
        self.assertEqual(out["frozen"], {"host": "pi", "session_id": "s1", "lock_id": "L1", "request_id": "r1"})
        # A lock with the same ids on another host is another target's.
        self.assertEqual(out["valid"], [True, False, False, False, False, False])
        self.assertIsNone(self.run_node(target=FREE)["frozen"])

    def test_spoken_ids_pruned_to_current_history(self):
        out = self.run_node(ids=["a", "b", "c"], requests=[{"request_id": "b"}])
        self.assertEqual(out["ids"], ["b"])

    def test_waiting_banner_settles_on_state_or_eviction_only_after_the_ack_snapshot(self):
        def snap(version, *requests):
            return {"version": version, "requests": [{"request_id": r, "state": s} for r, s in requests]}
        out = self.run_node(over=[
            ["r", snap(5, ("r", "handed_off")), 5],   # still waiting
            ["r", snap(6, ("r", "answered")), None],  # answered
            ["r", snap(6, ("r", "unknown")), None],   # left handed_off
            ["r", snap(4), None],                     # absent, no ack snapshot yet: keep waiting
            ["r", snap(4), 5],                        # absent in a snapshot older than the ack
            ["r", snap(5), 5],                        # absent in the ack snapshot: evicted
            ["r", snap(9), 5],                        # absent later: evicted
        ])
        self.assertEqual(out["over"], [False, True, True, False, False, True, True])

    def test_waiting_banner_settles_from_a_later_poll_when_the_ack_refresh_failed(self):
        def snap(version):
            return {"version": version, "requests": []}
        out = self.run_node(over=[
            ["r", snap(4), None, 3, 3],   # poll started before the ack: never settles
            ["r", snap(4), None, 2, 3],   # an older poll that returns late
            ["r", snap(7), None, 4, 3],   # first poll started after the ack, request evicted
            ["r", snap(7), None, None, 3],  # no poll start known: stays waiting
            ["r", snap(7), None, 4, None],  # no ack recorded: stays waiting
        ])
        self.assertEqual(out["over"], [False, False, True, False, False])


    def run_hosts(self, snapshot, keys=(), pairs=(), badges=()):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required by the suite")
        payload = {"snapshot": snapshot, "keys": list(keys), "pairs": list(pairs), "badges": list(badges)}
        proc = subprocess.run([node, "-e", HOSTS_SCRIPT, str(LOGIC)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_history_and_speech_filter_by_host_and_session_id(self):
        """The Tests table's "Hosts and enrollment" row: a request matches a
        target only when both its host and its session id do."""
        snapshot = {"targets": [{"host": "mac", "session_id": "s1"}, {"host": "pi", "session_id": "s2"}],
                    "requests": [{"request_id": "r1", "host": "pi", "session_id": "s1"},
                                 {"request_id": "r2", "host": "mac", "session_id": "s1"},
                                 {"request_id": "r3", "host": "pi", "session_id": "s2"},
                                 {"request_id": "r4", "host": "mac", "session_id": "s2"}]}
        pi_s1, mac_s1 = {"host": "pi", "session_id": "s1"}, {"host": "mac", "session_id": "s1"}
        out = self.run_hosts(snapshot, keys=[pi_s1, mac_s1, {"host": "pi", "session_id": "s2"}, None],
                             pairs=[[pi_s1, mac_s1], [mac_s1, dict(mac_s1)], [None, None], [pi_s1, None]])
        self.assertEqual(out["keys"], [mac_s1, {"host": "pi", "session_id": "s2"}])
        self.assertEqual(out["found"], [None, snapshot["targets"][0], snapshot["targets"][1], None])
        self.assertEqual(out["history"], [["r1"], ["r2"], ["r3"], []])
        self.assertEqual(out["same"], [False, True, False, False])

    def test_reassignment_never_shows_the_old_host_s_reply(self):
        """s1 was the Pi's and is now the Mac's: the Pi selection finds no
        target, and the Mac target's history holds only its own requests."""
        before = {"targets": [{"host": "pi", "session_id": "s1"}],
                  "requests": [{"request_id": "r1", "host": "pi", "session_id": "s1"}]}
        after = {"targets": [{"host": "mac", "session_id": "s1"}],
                 "requests": before["requests"] + [{"request_id": "r2", "host": "mac", "session_id": "s1"}]}
        pi_s1 = {"host": "pi", "session_id": "s1"}
        self.assertEqual(self.run_hosts(before, keys=[pi_s1])["history"], [["r1"]])
        out = self.run_hosts(after, keys=[pi_s1, {"host": "mac", "session_id": "s1"}])
        self.assertEqual(out["found"][0], None)
        self.assertEqual(out["history"][1], ["r2"])

    def test_badges_for_an_id_two_hosts_own_and_a_mac_target_not_connected(self):
        out = self.run_hosts({"targets": [], "requests": []}, badges=[
            {"host": "pi", "registration": "none", "lock": None},
            {"host": "mac", "registration": "none", "lock": None},
            {"host": "mac", "registration": "registered", "lock": None},
            {"host": "pi", "registration": "none", "lock": None, "enrolled_on_two_hosts": True},
            {"host": "pi", "registration": "none", "lock": {"earlier_run": False}, "enrolled_on_two_hosts": True},
        ])["badges"]
        self.assertEqual([b["text"] for b in out], ["Unavailable", "Not connected", "Registered, no desk reply yet",
                                                    "Enrolled on two hosts", "Busy"])
        self.assertEqual(out[3]["tone"], "bad")


if __name__ == "__main__":
    unittest.main()
