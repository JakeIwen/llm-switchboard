import unittest

from llm_switchboard.supervisor_state import (
    BACKOFF_MAX_S, NO_RC_RESTART_MAX_S, RC_GRACE_S, ScreenInput, State, backoff_s,
    no_rc_restart_delay_s, observe_screen,
)


class SupervisorStateTest(unittest.TestCase):
    def test_supervisor_public_operations_match_capture_contracts(self):
        from supervisor_oracle import capture
        self.assertEqual(len(capture()), 9)

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual([backoff_s(n) for n in range(5)], [0, 10, 20, 40, 80])
        self.assertEqual(backoff_s(100), BACKOFF_MAX_S)

    def test_screen_states_preserve_prompt_and_remote_control_precedence(self):
        cases = [
            ("/rc active", 0, State.RUNNING, "Remote Control active.", True),
            ("/rc failed", 0, State.ATTENTION,
             "Remote Control failed to connect; try Restart.", False),
            ("/rc connecting…", 0, State.STARTING,
             "Remote Control connecting.", False),
            ("/rc reconnecting...", 0, State.STARTING,
             "Remote Control reconnecting.", False),
            ("", RC_GRACE_S - 0.01, State.STARTING,
             "Launched; waiting for Remote Control.", False),
            ("❯ \n  ~/dev · Opus", RC_GRACE_S, State.ATTENTION,
             "Running, but Remote Control is off (no indicator on its status line). Restart usually "
             "brings it back; also quit any copy of this session opened without claudepd.", False),
            ("/rc active\nYes, I trust this folder", 0, State.ATTENTION,
             "Folder trust prompt is waiting; attach to answer it.", True),
        ]
        for screen, elapsed, state, detail, active in cases:
            with self.subTest(screen=screen, elapsed=elapsed):
                observed = observe_screen(ScreenInput(screen, elapsed))
                self.assertEqual((observed.state, observed.detail, observed.rc_active),
                                 (state, detail, active))

    def test_only_a_missing_or_failed_indicator_calls_for_a_restart(self):
        lost = {screen: observe_screen(ScreenInput(screen, RC_GRACE_S)).rc_lost
                for screen in ("❯ ", "/rc failed", "/rc active", "/rc reconnecting…", "Enter to confirm", " \n")}
        self.assertEqual(lost, {"❯ ": True, "/rc failed": True, "/rc active": False,
                                "/rc reconnecting…": False, "Enter to confirm": False, " \n": False})

    def test_automatic_restarts_back_off_to_a_cap(self):
        self.assertEqual([no_rc_restart_delay_s(n) for n in range(4)], [600, 1200, 2400, 3600])
        self.assertEqual(no_rc_restart_delay_s(50), NO_RC_RESTART_MAX_S)

    def test_registry_bridge_counts_as_connected_unless_the_screen_says_otherwise(self):
        cases = {"❯ ": State.RUNNING, "/rc failed": State.ATTENTION,
                 "/rc reconnecting…": State.STARTING, "Enter to confirm": State.ATTENTION}
        for screen, state in cases.items():
            with self.subTest(screen=screen):
                observed = observe_screen(ScreenInput(screen, RC_GRACE_S, rc_registered=True))
                self.assertEqual(observed.state, state)
                self.assertEqual(observed.rc_lost, screen == "/rc failed")
                self.assertEqual(observed.rc_active, screen in ("❯ ", "Enter to confirm"))

    def test_duplicate_process_is_appended_to_any_screen_detail(self):
        observed = observe_screen(ScreenInput("/rc active", 0, duplicate_pid=42))
        self.assertEqual(observed.detail,
                         "Remote Control active. Also open in another claude process (pid 42).")


if __name__ == "__main__":
    unittest.main()
