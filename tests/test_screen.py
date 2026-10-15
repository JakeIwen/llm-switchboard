import unittest

from claude_always_remote.screen import Rc, blocking_prompt, rc_indicator

BAR = "─" * 60
IDLE = f"""❯ Reply with just OK
● OK
{BAR}
❯
{BAR}
  ~/dev/x · main · Opus 5.5 (1M context) high · Fast off · ctx 3%                     ● high · /effort
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents                              /rc active
"""
# Captured from a session in the middle of a turn: the indicator shrinks to "/rc".
BUSY = f"""  ⎿  Running… (8s)
· Warping… (2m 18s · ↓ 9.7k tokens)
{BAR}
❯
{BAR}
  ~/dev/…/dashboard · master · Telemetrics app changes · Opus 5.5 (1M context) high · Fast off · ctx 10%
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
                         Still working. Check in from your phone · https://claude.ai/code/session_01KN
                                                                                                   /rc
"""


class ScreenTest(unittest.TestCase):
    def test_idle_and_busy_both_read_as_active(self):
        self.assertEqual(rc_indicator(IDLE), Rc.ACTIVE)
        self.assertEqual(rc_indicator(BUSY), Rc.ACTIVE)

    def test_other_indicator_states(self):
        for word in ("connecting", "reconnecting", "failed"):
            self.assertEqual(rc_indicator(IDLE.replace("/rc active", f"/rc {word}")), Rc(word))

    def test_no_indicator(self):
        self.assertIsNone(rc_indicator(IDLE.replace("/rc active", "")))

    def test_typed_rc_command_in_scrollback_is_not_the_indicator(self):
        screen = "❯ /rc\n" + "\n".join(f"output line {i}" for i in range(12))
        self.assertIsNone(rc_indicator(screen))

    def test_trust_prompt(self):
        self.assertIn("trust", blocking_prompt("❯ No, exit\n  Yes, I trust this folder\nEnter to confirm"))
        self.assertIsNone(blocking_prompt(IDLE))


if __name__ == "__main__":
    unittest.main()
