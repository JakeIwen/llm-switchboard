import unittest

from llm_switchboard.supervisor_state import (
    BACKOFF_MAX_S, RC_GRACE_S, ScreenInput, State, backoff_s, observe_screen,
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
            ("", RC_GRACE_S, State.ATTENTION,
             "Running, but no Remote Control indicator on its status line. Check the screen.", False),
            ("/rc active\nYes, I trust this folder", 0, State.ATTENTION,
             "Folder trust prompt is waiting; attach to answer it.", True),
        ]
        for screen, elapsed, state, detail, active in cases:
            with self.subTest(screen=screen, elapsed=elapsed):
                observed = observe_screen(ScreenInput(screen, elapsed))
                self.assertEqual((observed.state, observed.detail, observed.rc_active),
                                 (state, detail, active))

    def test_duplicate_process_is_appended_to_any_screen_detail(self):
        observed = observe_screen(ScreenInput("/rc active", 0, duplicate_pid=42))
        self.assertEqual(observed.detail,
                         "Remote Control active. Also open in another claude process (pid 42).")


if __name__ == "__main__":
    unittest.main()
