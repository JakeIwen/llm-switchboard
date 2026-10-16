import shlex
import tempfile
import unittest
from pathlib import Path

from llm_switchboard.live_registry import LiveProcess
from llm_switchboard.paths import MANAGED_ENV_VAR
from llm_switchboard.store import Store
from llm_switchboard.supervisor import RC_GRACE_S, State, Supervisor

from fakes import SID, SID2, Clock, FakeConnectivity, FakeIndex, FakeTmux, transcript


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "sessions.json")
        self.tmux = FakeTmux()
        self.net = FakeConnectivity()
        self.clock = Clock()
        self.live: list[LiveProcess] = []
        self.sup = Supervisor(self.store, FakeIndex(transcript(), transcript(SID2, "other")), self.tmux,
                              self.net, registry=lambda: self.live, clock=self.clock)

    def state(self, sid=SID):
        return self.sup.snapshot()[sid].state

    def test_launches_enabled_session_through_claudepd_with_remote_control(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.assertEqual(self.state(), State.STARTING)
        [(sid, command, env)] = self.tmux.started
        self.assertEqual(sid, SID)
        self.assertEqual(env, {MANAGED_ENV_VAR: SID})
        argv = shlex.split(command)
        self.assertEqual(argv[:3], ["exec", "bash", "-lic"])
        inner = shlex.split(argv[3])
        self.assertIn("claudepd", inner)
        self.assertEqual(inner[inner.index("--resume") + 1], SID)
        self.assertEqual(inner[inner.index("--remote-control") + 1], "Telemetrics app changes")
        self.assertEqual(inner[:3], ["cd", "--", "/home/pi/dev/obd-things"])

    def test_custom_name_is_used_for_remote_control(self):
        self.store.set_enabled(SID, True)
        self.store.set_name(SID, "van telemetrics")
        self.sup.reconcile()
        inner = shlex.split(shlex.split(self.tmux.started[0][1])[3])
        self.assertEqual(inner[inner.index("--remote-control") + 1], "van telemetrics")

    def test_running_once_rc_marker_shows(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.tmux.screens[self.tmux.window_for(SID).window_id] = "… /rc active"
        self.sup.reconcile()
        self.assertEqual(self.state(), State.RUNNING)
        self.assertEqual(len(self.tmux.started), 1)

    def test_attention_when_rc_never_shows_or_prompt_waits(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.clock.t += RC_GRACE_S + 1
        self.sup.reconcile()
        self.assertEqual(self.state(), State.ATTENTION)
        self.tmux.screens[self.tmux.window_for(SID).window_id] = "Yes, I trust this folder\nEnter to confirm"
        self.sup.reconcile()
        self.assertIn("trust", self.sup.snapshot()[SID].detail)

    def test_quick_exit_backs_off_then_relaunches(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.tmux.exit(SID, 1)
        self.sup.reconcile()
        status = self.sup.snapshot()[SID]
        self.assertEqual(status.state, State.BACKOFF)
        self.assertEqual(status.last_exit.status, 1)
        self.assertEqual(self.tmux.wins, {})
        self.sup.reconcile()
        self.assertEqual(len(self.tmux.started), 1)
        self.clock.t = status.next_attempt
        self.sup.reconcile()
        self.assertEqual(len(self.tmux.started), 2)

    def test_repeated_failures_grow_the_backoff(self):
        self.store.set_enabled(SID, True)
        delays = []
        for _ in range(3):
            self.sup.reconcile()
            self.tmux.exit(SID, 1)
            self.sup.reconcile()
            status = self.sup.snapshot()[SID]
            delays.append(status.next_attempt - self.clock.t)
            self.clock.t = status.next_attempt
        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[2])

    def test_disabling_closes_the_window(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.store.set_enabled(SID, False)
        self.sup.reconcile()
        self.assertEqual(self.tmux.wins, {})
        self.assertNotIn(SID, self.sup.snapshot())

    def test_waits_while_open_in_another_process(self):
        self.live = [LiveProcess(pid=42, session_id=SID, cwd="", status="idle", managed_for=None, rss_kb=1)]
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.assertEqual(self.state(), State.ELSEWHERE)
        self.assertEqual(self.tmux.started, [])
        self.live = []
        self.sup.reconcile()
        self.assertEqual(len(self.tmux.started), 1)

    def test_own_process_is_not_mistaken_for_another(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.live = [LiveProcess(pid=7, session_id=SID, cwd="", status="busy", managed_for=SID, rss_kb=2048)]
        self.sup.reconcile()
        status = self.sup.snapshot()[SID]
        self.assertFalse(status.duplicate)
        self.assertTrue(status.busy)
        self.assertEqual(status.pid, 7)

    def test_waits_for_network(self):
        self.net.up = False
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        self.assertEqual(self.state(), State.OFFLINE)
        self.assertEqual(self.tmux.started, [])

    def test_restart_request_relaunches_in_one_pass(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        first = self.tmux.window_for(SID).window_id
        self.sup.request_restart(SID)
        self.sup.reconcile()
        self.assertIn(first, self.tmux.killed)
        self.assertEqual(len(self.tmux.started), 2)

    def test_missing_transcript(self):
        self.store.set_enabled("11111111-2222-3333-4444-555555555555", True)
        self.sup.reconcile()
        self.assertEqual(self.state("11111111-2222-3333-4444-555555555555"), State.MISSING)

    def test_adopts_windows_left_by_a_previous_run(self):
        self.store.set_enabled(SID, True)
        self.sup.reconcile()
        fresh = Supervisor(self.store, self.sup.index, self.tmux, self.net, registry=lambda: [], clock=self.clock)
        fresh.reconcile()
        self.assertEqual(len(self.tmux.started), 1)


if __name__ == "__main__":
    unittest.main()
