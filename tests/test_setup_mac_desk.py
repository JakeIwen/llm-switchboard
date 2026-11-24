"""scripts/setup-mac-desk.sh (docs/voice-desk.md, "Installing the Mac side"
and "Switching it off, revocation and rollback"): the order of its steps,
its checks before any change, and `--remove`'s order.

The script runs from a copy in a temporary checkout, under an isolated HOME
and a PATH whose `ssh`, `ssh-keygen`, `launchctl`, `llm-switchboard` and
`uname` are stand-ins that log their arguments (the checkout's `.venv/bin/python`
is one too), so nothing here reaches the Pi, launchd or the real ~/.ssh. The
Pi alias in the stand-in local.env is under `.invalid`, so even a real ssh
could resolve nothing.
"""

import os
from pathlib import Path
import shutil
import subprocess
import unittest

from desk_fixtures import make_home

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "setup-mac-desk.sh"
PI = "pi.invalid"
PI_CLI = "/home/pi/dev/llm-switchboard/bin/llm-switchboard"
STANDIN = """#!/bin/sh
printf '%s\\n' "{name} $*" >> "$STANDIN_LOG"
case "{name} $*" in ${{STANDIN_FAIL:-//never//}}) exit "${{STANDIN_FAIL_CODE:-1}}";; esac
"""
STANDINS = {
    "uname": "echo \"${STANDIN_UNAME:-Darwin}\"\n",
    "ssh": "input=$(cat)\n[ -z \"$input\" ] || printf 'stdin %s\\n' \"$input\" >> \"$STANDIN_LOG\"\n",
    "ssh-keygen": (
        "out=''; make=0; sign=0; last=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case $1 in -f) out=$2; shift;; -t) make=1; shift;; -Y) [ \"$2\" = sign ] && sign=1; shift;; esac\n"
        "  last=$1; shift\n"
        "done\n"
        "if [ $make = 1 ]; then echo private > \"$out\"; echo \"ssh-ed25519 AAAAfake ${out##*/}\" > \"$out.pub\"; fi\n"
        "if [ $sign = 1 ]; then echo signature > \"$last.sig\"; fi\n"
    ),
    "launchctl": "",
    "llm-switchboard": "",
}


class SetupMacDeskTest(unittest.TestCase):
    def setUp(self):
        root = make_home(self)
        self.home = root / "home"
        self.home.mkdir()
        self.repo = root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.repo / "scripts" / SCRIPT.name)
        (self.repo / "local.env").write_text(f"LLM_SWITCHBOARD_PI_SSH={PI}\nLLM_SWITCHBOARD_MAC_NAME=mac\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in STANDINS.items():
            self._standin(self.bin / name, name, body)
        self._standin(self.repo / ".venv" / "bin" / "python", "python", "")
        self.log = root / "calls.log"
        self.key = self.home / ".ssh" / "llm-switchboard-desk-mac"

    def _standin(self, path: Path, name: str, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STANDIN.format(name=name) + body)
        path.chmod(0o755)

    def run_script(self, *args, **env):
        environ = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "STANDIN_LOG": str(self.log),
                   **env}
        result = subprocess.run([str(self.repo / "scripts" / SCRIPT.name), *args], env=environ,
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return result, [call for call in calls if not call.startswith("uname ")]

    def add_key(self):
        self.key.parent.mkdir(mode=0o700)
        self.key.write_text("private\n")
        self.key.with_name(self.key.name + ".pub").write_text("ssh-ed25519 AAAAkept llm-switchboard-desk-mac\n")

    def assert_changed_nothing(self, calls):
        changes = (f"ssh-keygen -q -t ed25519 -N  -C llm-switchboard-desk-mac -f {self.key}",
                   f"ssh {PI} {PI_CLI} desk link-authorize mac", "llm-switchboard ", "python ", "launchctl ")
        for call in calls:
            self.assertFalse(call.startswith(changes), call)
        self.assertFalse(self.key.exists())

    def test_setup_steps_in_order(self):
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        throwaway = calls[0].rsplit(" -f ", 1)[1]
        self.assertEqual(calls, [
            f"ssh-keygen -q -t ed25519 -N  -C desk-self-test -f {throwaway}",
            f"ssh-keygen -Y sign -f {throwaway} -n llm-switchboard-desk-link {Path(throwaway).parent}/message",
            f"ssh-keygen -Y verify -f {Path(throwaway).parent}/signers -I self-test -n llm-switchboard-desk-link "
            f"-s {Path(throwaway).parent}/message.sig",
            f"ssh {PI} {PI_CLI} desk link-authorize --self-test",
            f"ssh-keygen -q -t ed25519 -N  -C llm-switchboard-desk-mac -f {self.key}",
            f"ssh {PI} {PI_CLI} desk link-authorize mac",
            f"stdin ssh-ed25519 AAAAfake {self.key.name}",
            "llm-switchboard desk install-config",
            "python -m llm_switchboard desk relay-install",
            f"launchctl print gui/{os.getuid()}/dev.llm-switchboard.desk-relay",
        ])
        self.assertFalse(Path(throwaway).parent.exists(), "the throwaway key's directory is deleted")
        self.assertEqual(self.key.parent.stat().st_mode & 0o777, 0o700)

    def test_setup_keeps_an_existing_key(self):
        self.add_key()
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"ssh-keygen -q -t ed25519 -N  -C llm-switchboard-desk-mac -f {self.key}", calls)
        self.assertIn("stdin ssh-ed25519 AAAAkept llm-switchboard-desk-mac", calls)
        self.assertEqual(self.key.read_text(), "private\n")

    def test_setup_failed_check_stops_before_any_change(self):
        cases = {
            "not macOS": ({"STANDIN_UNAME": "Linux"}, "runs only on the Mac"),
            "the Mac's ssh-keygen can't verify": ({"STANDIN_FAIL": "ssh-keygen -Y verify*"},
                                                  "this Mac's ssh-keygen can't sign and verify"),
            "the Mac's ssh-keygen can't sign": ({"STANDIN_FAIL": "ssh-keygen -Y sign*"},
                                                "this Mac's ssh-keygen can't sign and verify"),
            "the Pi's OpenSSH is too old": ({"STANDIN_FAIL": "ssh * --self-test", "STANDIN_FAIL_CODE": "2"},
                                            "the Pi's ssh-keygen can't verify SSH signatures"),
            "the Pi doesn't answer": ({"STANDIN_FAIL": "ssh * --self-test", "STANDIN_FAIL_CODE": "255"},
                                      "failed (exit 255)"),
        }
        for name, (env, message) in cases.items():
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                result, calls = self.run_script(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertIn("nothing changed", result.stderr)
                self.assert_changed_nothing(calls)

    def test_setup_refuses_without_names_or_checkout(self):
        cases = {
            "no Mac name": ("LLM_SWITCHBOARD_PI_SSH=pi.invalid\n", "LLM_SWITCHBOARD_MAC_NAME"),
            "pi as the Mac's name": ("LLM_SWITCHBOARD_PI_SSH=pi.invalid\nLLM_SWITCHBOARD_MAC_NAME=pi\n",
                                     "LLM_SWITCHBOARD_MAC_NAME"),
            "no Pi alias": ("LLM_SWITCHBOARD_MAC_NAME=mac\n", "LLM_SWITCHBOARD_PI_SSH"),
            "an option as the alias": ("LLM_SWITCHBOARD_PI_SSH=-oProxyCommand=x\nLLM_SWITCHBOARD_MAC_NAME=mac\n",
                                       "LLM_SWITCHBOARD_PI_SSH"),
        }
        for remove in ((), ("--remove",)):
            for name, (local_env, message) in cases.items():
                with self.subTest(name, remove=remove):
                    self.log.unlink(missing_ok=True)
                    (self.repo / "local.env").write_text(local_env)
                    result, calls = self.run_script(*remove)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(calls, [])
        (self.repo / "local.env").write_text(f"LLM_SWITCHBOARD_PI_SSH={PI}\nLLM_SWITCHBOARD_MAC_NAME=mac\n")
        (self.repo / ".venv" / "bin" / "python").unlink()
        self.log.unlink(missing_ok=True)
        result, calls = self.run_script()
        self.assertIn(".venv/bin/python is missing", result.stderr)
        self.assertEqual(calls, [])

    def test_remove_in_order(self):
        self.add_key()
        mcp = self.home / ".config" / "llm-switchboard" / "desk" / "mcp.json"
        mcp.parent.mkdir(parents=True)
        mcp.write_text("{}\n")
        result, calls = self.run_script("--remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["python -m llm_switchboard desk relay-install --unload",
                                 f"ssh {PI} {PI_CLI} desk link-revoke mac"])
        self.assertFalse(self.key.exists())
        self.assertFalse(self.key.with_name(self.key.name + ".pub").exists())
        self.assertEqual(mcp.read_text(), "{}\n")

    def test_remove_stops_at_a_failed_step(self):
        cases = {
            "unload not confirmed": ("python * --unload", ["python -m llm_switchboard desk relay-install --unload"],
                                     "the relay's unload is not confirmed"),
            "link-revoke failed": ("ssh * link-revoke mac", ["python -m llm_switchboard desk relay-install --unload",
                                                             f"ssh {PI} {PI_CLI} desk link-revoke mac"],
                                   "link-revoke on the Pi failed"),
        }
        self.add_key()
        for name, (pattern, expected, message) in cases.items():
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                result, calls = self.run_script("--remove", STANDIN_FAIL=pattern)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(calls, expected)
                self.assertIn(message, result.stderr)
                self.assertTrue(self.key.exists())

    def test_script_is_executable_and_names_no_machine(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        text = SCRIPT.read_text()
        self.assertIn('. "$repo/local.env"', text)
        self.assertNotIn("${LLM_SWITCHBOARD_PI_SSH:-pi}", text)


if __name__ == "__main__":
    unittest.main()
