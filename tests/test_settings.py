import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest

from llm_switchboard import settings
from llm_switchboard.launcher import codex_resume_command, new_session_command, pane_command, resume_command


class SettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "state" / "settings.json"

    def test_defaults_without_writing_a_file(self):
        current = settings.load(self.path)
        self.assertEqual(current, {"claude_command": "claude", "codex_command": "codex",
                                   "folders": {"depth": 2, "min_sessions": 2, "hidden": []}, "poll_seconds": 4})
        current["folders"]["hidden"].append("~/private")
        self.assertEqual(settings.load(self.path)["folders"]["hidden"], [])
        self.assertFalse(self.path.exists())

    def test_partial_updates_preserve_other_values_and_round_trip(self):
        settings.save({"claude_command": "claudep", "folders": {"hidden": ["~/private"]}}, self.path)
        current = settings.save({"codex_command": "/bin/codex", "folders": {"depth": 0}, "poll_seconds": 60}, self.path)
        self.assertEqual(current, settings.load(self.path))
        self.assertEqual(json.loads(self.path.read_text()), current)
        self.assertEqual(current["folders"], {"depth": 0, "min_sessions": 2, "hidden": ["~/private"]})
        self.assertEqual(current["claude_command"], "claudep")

    def test_invalid_values_name_the_key_and_do_not_write(self):
        bad = {
            "claude_command": ["", "two words", "echo;true", "a\n", "$HOME/bin/a", "a" * 65, 1, None],
            "codex_command": ["$(oops)", True],
            "poll_seconds": [1, 61, 4.0, True, "4", None],
            "folders.depth": [-1, 7, False, "2"],
            "folders.min_sessions": [0, 51, 2.0],
            "folders.hidden": ["~/a", ["/tmp"], ["~/two words"], ["~/a\n"], [1], ["~/a"] * 51],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    parts = key.split(".")
                    update = {parts[-1]: value}
                    if len(parts) > 1:
                        update = {parts[0]: update}
                    with self.assertRaisesRegex(ValueError, key):
                        settings.save(update, self.path)
        self.assertFalse(self.path.exists())

    def test_validation_requires_objects_and_known_update_keys(self):
        for update, key in [([], "settings"), ({"folders": []}, "folders"),
                            ({"unknown": 2}, "unknown"), ({"folders": {"unknown": 2}}, "folders.unknown")]:
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, key):
                settings.validate(update)

    def test_valid_boundaries_and_shell_words(self):
        for command in ["claudep", "codex_2", "./bin/cli", "/opt/cli-v2.0", "a" * 64]:
            self.assertEqual(settings.validate({"claude_command": command}), {"claude_command": command})
        settings.validate({"folders": {"depth": 6, "min_sessions": 50, "hidden": ["~", "~/a/b"]}, "poll_seconds": 2})
        settings.validate({"folders": {"depth": 0, "min_sessions": 1, "hidden": ["~/a"] * 50}, "poll_seconds": 60})

    def test_corrupt_file_falls_back_with_warning(self):
        self.path.parent.mkdir()
        for content in [b"not json", b"[]", b"null", b"\xff"]:
            self.path.write_bytes(content)
            with self.assertLogs("llm_switchboard.settings", level="WARNING"):
                self.assertEqual(settings.load(self.path), settings.defaults())
        with self.assertLogs("llm_switchboard.settings", level="WARNING"):
            settings.save({"claude_command": "claudep"}, self.path)
        self.assertEqual(settings.load(self.path)["claude_command"], "claudep")

    def test_invalid_leaf_falls_back_without_losing_valid_siblings(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"claude_command": "claudep", "poll_seconds": 1,
                                         "folders": {"depth": 99, "hidden": ["~/private"]}, "old": True}))
        with self.assertLogs("llm_switchboard.settings", level="WARNING") as messages:
            loaded = settings.load(self.path)
        self.assertIn("poll_seconds", " ".join(messages.output))
        self.assertIn("folders.depth", " ".join(messages.output))
        self.assertEqual(loaded["poll_seconds"], 4)
        self.assertEqual(loaded["folders"], {"depth": 2, "min_sessions": 2, "hidden": ["~/private"]})
        self.assertEqual(loaded["claude_command"], "claudep")
        with self.assertLogs("llm_switchboard.settings", level="WARNING"):
            saved = settings.save({}, self.path)
        self.assertEqual(json.loads(self.path.read_text()), saved)
        self.assertNotIn("old", saved)

    def test_unknown_nested_file_keys_are_removed_on_save(self):
        self.path.parent.mkdir()
        self.path.write_text('{"folders":{"obsolete":1},"unknown":2}')
        self.assertEqual(settings.save({}, self.path), settings.defaults())
        self.assertEqual(json.loads(self.path.read_text()), settings.defaults())

    def test_concurrent_partial_updates_do_not_clobber_each_other(self):
        updates = [{"claude_command": "claudep"}, {"codex_command": "codexp"}, {"folders": {"depth": 5}},
                   {"folders": {"hidden": ["~/private"]}}, {"poll_seconds": 10}]
        with ThreadPoolExecutor(max_workers=len(updates)) as pool:
            list(pool.map(lambda update: settings.save(update, self.path), updates))
        self.assertEqual(settings.load(self.path), {"claude_command": "claudep", "codex_command": "codexp",
                                                   "folders": {"depth": 5, "min_sessions": 2, "hidden": ["~/private"]},
                                                   "poll_seconds": 10})

    def test_resume_commands_change_without_changing_kept_launches(self):
        current = settings.save({"claude_command": "claudep", "codex_command": "codexp"}, self.path)
        self.assertEqual(resume_command("sid", "/work/two words", current), "cd -- '/work/two words' && claudep --resume sid")
        self.assertEqual(codex_resume_command("sid", "", current), "codexp resume sid")
        self.assertEqual(resume_command("sid", ""), "claude --resume sid")
        self.assertEqual(codex_resume_command("sid", ""), "codex resume sid")
        self.assertIn("claudepd --resume sid", shlex.split(pane_command("sid", "/work", "name"))[-1])
        self.assertIn("claudepd --session-id sid", shlex.split(new_session_command("sid", "/work", "title", "name"))[-1])


class SettingsCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = dict(os.environ, HOME=tmp.name, XDG_CONFIG_HOME="", XDG_DATA_HOME="", LLM_SWITCHBOARD_CODEX_SOCKET="")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "llm_switchboard", "settings", *args],
                              cwd=Path(__file__).resolve().parents[1], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_show_set_and_nested_keys(self):
        self.assertEqual(json.loads(self.run_cli().stdout), settings.defaults())
        for key, value in [("claude_command", "claudep"), ("codex_command", "codexp"),
                           ("folders.depth", "0"), ("folders.hidden", "~/private, ~/archive, "), ("poll_seconds", "12")]:
            result = self.run_cli("set", key, value)
            self.assertEqual(result.returncode, 0, result.stderr)
        current = json.loads(self.run_cli().stdout)
        self.assertEqual(current["folders"], {"depth": 0, "min_sessions": 2, "hidden": ["~/private", "~/archive"]})
        self.assertEqual(current["claude_command"], "claudep")
        self.assertEqual(current["codex_command"], "codexp")
        self.assertEqual(current["poll_seconds"], 12)
        path = self.root / ".config" / "llm-switchboard" / "settings.json"
        self.assertEqual(json.loads(path.read_text()), current)
        self.assertEqual(json.loads(self.run_cli("set", "folders.hidden", "").stdout)["folders"]["hidden"], [])

    def test_errors_are_actionable_without_tracebacks(self):
        for key, value in [("unknown", "3"), ("folders", "3"), ("folders.hidden", "/tmp"),
                           ("folders.depth", "two"), ("poll_seconds", "1"), ("claude_command", "a b")]:
            with self.subTest(key=key):
                result = self.run_cli("set", key, value)
                self.assertEqual(result.returncode, 2)
                self.assertIn(key, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("set", result.stdout)
