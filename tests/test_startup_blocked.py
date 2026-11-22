"""Display-only recognition of Claude Code's development-channel startup warning."""

from pathlib import Path
import unittest

from llm_switchboard.screen import desk_startup_warning
from llm_switchboard.supervisor_state import ScreenInput, State, observe_screen

# Captured from the visible Pi pane at 120 columns; contains no session data.
WARNING = (Path(__file__).parent / "screens" / "desk-channel-warning.txt").read_text()


class StartupBlockedTest(unittest.TestCase):
    def test_visible_warning_blocks_remote_control_even_after_restart_grace(self):
        for screen in (WARNING, "\n" + WARNING + "\n" * 26,
                       WARNING.replace("to run\n  channels", "to run channels"),
                       WARNING.replace("❯ 1.", "  1.").replace("  2. Exit", "❯ 2. Exit")):
            with self.subTest(screen=screen):
                self.assertTrue(desk_startup_warning(screen))
                observed = observe_screen(ScreenInput(screen, 3600, rc_registered=True))
                self.assertIs(observed.state, State.STARTUP_BLOCKED)
                self.assertFalse(observed.rc_lost)
                self.assertFalse(observed.rc_active)
                self.assertIn("attach", observed.detail)

    def test_prompt_history_and_other_dialogs_are_not_this_warning(self):
        for screen in ("❯", "❯ Explain WARNING: Loading development channels",
                       "Allow Bash to run this command?\n❯ 1. Yes\n2. No\nEnter to confirm",
                       "Do you trust this folder?\n❯ Yes\nEnter to confirm",
                       "Security notes:\nPress Enter to continue", WARNING + "❯ ",
                       " ".join(WARNING.split()),
                       "Here is the warning you asked for:\n" + WARNING,
                       WARNING.replace("server:desk", "server:desk server:other"),
                       WARNING.replace("2. Exit", "2. Allow always"),
                       WARNING.split("Channels: server:desk")[-1], " " * 16_384 + WARNING):
            with self.subTest(screen=screen[:64]):
                self.assertFalse(desk_startup_warning(screen))
                self.assertIsNot(observe_screen(ScreenInput(screen, 50)).state, State.STARTUP_BLOCKED)
