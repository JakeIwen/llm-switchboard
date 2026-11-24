"""The Mac's `llm-switchboard desk` commands through the real CLI, each a
separate process under an isolated HOME: `flags --lines` (the contract
`claudep --desk` reads in the Mac shell config) and `relay-install
[--unload]`.

The relay-install runs put a stand-in `launchctl` first on PATH that logs its
arguments and answers as launchctl does for a job that isn't loaded, so no
test reaches launchd; HOME is temporary, so nothing is written under the
real ~/Library.
"""

import os
from pathlib import Path
import sys
import unittest

from desk_fixtures import desk_config, make_home, run_desk_cli
from llm_switchboard.desk.relay_launchd import LABEL
from llm_switchboard.local_config import MAC_NAME

LAUNCHCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$LAUNCHCTL_LOG"
case $1 in print) exit 113;; bootout) exit 3;; esac
exit 1
"""


class FlagsLinesTest(unittest.TestCase):
    def test_flags_lines_prints_one_flag_per_line(self):
        home = make_home(self) / "a home"
        home.mkdir()
        flags = desk_config(home).launch_flags
        result = run_desk_cli(home, "flags", "--lines")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), list(flags))
        # claudep --desk refuses anything but four lines, and a path with a
        # space stays one argument.
        self.assertEqual(len(result.stdout.splitlines()), 4)
        self.assertIn(f"{home}/.config/llm-switchboard/desk/mcp.json", result.stdout.splitlines())
        self.assertEqual(run_desk_cli(home, "flags").stdout, " ".join(flags) + "\n")


class RelayInstallCliTest(unittest.TestCase):
    def setUp(self):
        self.home = make_home(self)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        (self.bin / "launchctl").write_text(LAUNCHCTL)
        (self.bin / "launchctl").chmod(0o755)
        self.log = self.home / "launchctl.log"

    def run_cli(self, *args):
        env = {"PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "LAUNCHCTL_LOG": str(self.log),
               MAC_NAME: "mac"}
        result = run_desk_cli(self.home, *args, env=env)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, calls

    def test_relay_install_without_key_refused_before_launchctl(self):
        result, calls = self.run_cli("relay-install")
        self.assertEqual(result.returncode, 2, result.stderr)
        expected = "llm-switchboard-desk-mac is missing" if sys.platform == "darwin" else "runs only on the Mac"
        self.assertIn(expected, result.stderr)
        self.assertEqual(calls, [])
        self.assertFalse((self.home / "Library").exists())

    def test_relay_install_unload_through_cli(self):
        result, calls = self.run_cli("relay-install", "--unload")
        if sys.platform != "darwin":
            self.assertEqual(result.returncode, 2)
            self.assertIn("runs only on the Mac", result.stderr)
            self.assertEqual(calls, [])
            return
        self.assertEqual(result.returncode, 0, result.stderr)
        service = f"gui/{os.getuid()}/{LABEL}"
        self.assertEqual(calls, [f"bootout {service}", f"print {service}"])
        self.assertIn("no desk relay is running", result.stdout)
        self.assertFalse(Path(self.home / "Library" / "LaunchAgents").exists())


if __name__ == "__main__":
    unittest.main()
