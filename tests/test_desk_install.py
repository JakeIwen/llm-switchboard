"""install.sh's voice desk reconciliation, against fake systemctl/loginctl/uv/tailscale and a temp HOME."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"
HUB = "llm-switchboard-desk.service"
ADB = "llm-switchboard-desk-adb.service"

FAKE_SYSTEMCTL = """#!/bin/bash
echo "$*" >> "$FAKE_DIR/systemctl.log"
shift  # --user
verb=$1; shift
flags=(); units=()
for a in "$@"; do case "$a" in -*) flags+=("$a");; *) units+=("$a");; esac; done
unit=${units[0]:-}
case "$verb" in
  is-active) [ -f "$FAKE_DIR/active/$unit" ] ;;
  is-enabled) [ -f "$FAKE_DIR/enabled/$unit" ] ;;
  enable) touch "$FAKE_DIR/enabled/$unit"; [[ " ${flags[*]} " == *" --now "* ]] && touch "$FAKE_DIR/active/$unit"; true ;;
  disable) rm -f "$FAKE_DIR/enabled/$unit"; [[ " ${flags[*]} " == *" --now "* ]] && rm -f "$FAKE_DIR/active/$unit"; true ;;
  restart) touch "$FAKE_DIR/active/$unit" ;;
  *) true ;;
esac
"""


class DeskInstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.fake = self.root / "fake"
        bin_dir = self.root / "bin"
        for d in (self.home, bin_dir, self.fake / "active", self.fake / "enabled"):
            d.mkdir(parents=True)
        scripts = {"systemctl": FAKE_SYSTEMCTL, "loginctl": "#!/bin/bash\necho yes\n",
                   "uv": "#!/bin/bash\nexit 0\n", "tailscale": "#!/bin/bash\necho 100.0.0.1\n"}
        for name, body in scripts.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "FAKE_DIR": str(self.fake),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}", "USER": "tester",
                    "LLM_SWITCHBOARD_CODEX_SOCKET": ""}
        self.local_env = self.root / "local.env"
        tls = self.home / ".config/llm-switchboard/desk/tls"
        tls.mkdir(parents=True)
        (tls / "leaf.pem").write_text("pem")
        (tls / "leaf.key").write_text("key")

    def desk_units(self):
        return subprocess.run([str(REPO / "scripts" / "desk-units.sh"), str(REPO), str(self.local_env)],
                              env=self.env, capture_output=True, text=True, timeout=60, check=False)

    def calls(self):
        log = self.fake / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []

    def clear_calls(self):
        (self.fake / "systemctl.log").unlink(missing_ok=True)

    def set_serial(self, serial):
        self.local_env.write_text(f"DESK_ADB_SERIAL={serial}\n")

    def test_first_run_enables_both_units(self):
        self.set_serial("TAB1")
        result = self.desk_units()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--user enable --now {HUB}", self.calls())
        self.assertIn(f"--user enable --now {ADB}", self.calls())
        self.assertIn("enabled and started", result.stdout)

    def test_rerun_restarts_active_hub_but_leaves_unchanged_adb_alone(self):
        self.set_serial("TAB1")
        self.desk_units()
        self.clear_calls()
        result = self.desk_units()
        self.assertIn(f"--user restart {HUB}", self.calls(), "a rotated leaf needs a hub restart")
        self.assertNotIn(f"--user restart {ADB}", self.calls())
        self.assertIn("restarted", result.stdout)
        self.assertIn("left alone", result.stdout)

    def test_changed_serial_restarts_adb_unit(self):
        self.set_serial("TAB1")
        self.desk_units()
        self.set_serial("TAB2")
        self.clear_calls()
        result = self.desk_units()
        self.assertIn(f"--user restart {ADB}", self.calls())
        self.assertIn("serial or unit changed", result.stdout)

    def test_changed_unit_file_restarts_adb_unit(self):
        self.set_serial("TAB1")
        self.desk_units()
        unit = self.home / ".config/systemd/user" / ADB
        unit.write_text(unit.read_text() + "# stale\n")
        self.clear_calls()
        self.desk_units()
        self.assertIn(f"--user restart {ADB}", self.calls())

    def test_emptied_serial_disables_and_stops_adb_unit(self):
        self.set_serial("TAB1")
        self.desk_units()
        self.set_serial("")
        self.clear_calls()
        result = self.desk_units()
        self.assertIn(f"--user disable --now {ADB}", self.calls())
        self.assertFalse((self.fake / "active" / ADB).exists())
        self.assertIn("disabled and stopped", result.stdout)
        # And a further run with no serial and nothing enabled just says how to enable it.
        self.clear_calls()
        again = self.desk_units()
        self.assertNotIn(f"--user disable --now {ADB}", self.calls())
        self.assertIn("Tablet link not enabled", again.stdout)

    def test_missing_leaf_leaves_hub_alone_and_says_why(self):
        (self.home / ".config/llm-switchboard/desk/tls/leaf.key").unlink()
        self.set_serial("")
        result = self.desk_units()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(HUB, " ".join(self.calls()).replace("daemon-reload", ""))
        self.assertIn("Voice desk hub not enabled", result.stderr)

    def test_install_sh_runs_the_picker_steps_then_the_desk_reconcile(self):
        self.set_serial("TAB1")
        # install.sh reads <repo>/local.env; a copy of the repo scripts keeps the checkout untouched.
        copy = self.root / "repo"
        subprocess.run(["rsync", "-a", "--exclude=.git", "--exclude=.venv", "--exclude=node_modules",
                        f"{REPO}/", f"{copy}/"], check=True)
        (copy / "local.env").write_text("DESK_ADB_SERIAL=TAB1\n")
        result = subprocess.run([str(copy / "install.sh")], env=self.env, capture_output=True,
                                text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertIn("--user enable --now llm-switchboard.service", calls)
        self.assertIn("--user restart llm-switchboard.service", calls)
        self.assertIn(f"--user enable --now {HUB}", calls)
        self.assertIn(f"--user enable --now {ADB}", calls)
        self.assertIn("Tablet link: enabled and started", result.stdout)
        self.assertTrue((self.home / ".config/systemd/user/llm-switchboard.service").is_file())


if __name__ == "__main__":
    unittest.main()
