"""install.sh's voice desk reconciliation, against fake systemctl/loginctl/uv/tailscale.

Everything runs against a fixture: a temp HOME, fixture XDG roots, and a copy of
the repo's files (regular files only, so no symlink can lead a write out of it)."""

import os
import shutil
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
        self.bin_dir = bin_dir
        xdg = {f"XDG_{name}_HOME": str(self.root / f"xdg-{name.lower()}")
               for name in ("CONFIG", "STATE", "DATA", "CACHE")}
        xdg["XDG_RUNTIME_DIR"] = str(self.root / "xdg-runtime")
        for path in xdg.values():
            Path(path).mkdir()
        self.env = {**os.environ, **xdg, "HOME": str(self.home), "FAKE_DIR": str(self.fake),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}", "USER": "tester",
                    "LLM_SWITCHBOARD_CODEX_SOCKET": ""}
        self.repo = self.root / "repo"
        self.copy_repo_files(self.repo)
        self.local_env = self.repo / "local.env"
        tls = Path(self.env["XDG_CONFIG_HOME"]) / "llm-switchboard/desk/tls"  # where the hub reads it
        tls.mkdir(parents=True)
        (tls / "leaf.pem").write_text("pem")
        (tls / "leaf.key").write_text("key")

    @staticmethod
    def copy_repo_files(dest):
        """Copies tracked and untracked-not-ignored regular files, never following a symlink."""
        listing = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                 cwd=REPO, capture_output=True, check=True).stdout
        for name in filter(None, listing.decode().split("\0")):
            source = REPO / name
            if source.is_symlink() or not source.is_file():
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def add_fake_tool(self, name, body):
        (self.bin_dir / name).write_text(body)
        (self.bin_dir / name).chmod(0o755)

    def desk_units(self):
        return subprocess.run([str(self.repo / "scripts" / "desk-units.sh"), str(self.repo), str(self.local_env)],
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
        self.assertIn("serial, script or unit changed", result.stdout)

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
        (Path(self.env["XDG_CONFIG_HOME"]) / "llm-switchboard/desk/tls/leaf.key").unlink()
        self.set_serial("")
        result = self.desk_units()
        self.assertEqual(result.returncode, 0, result.stderr)
        for verb in ("enable", "disable", "restart"):
            self.assertNotIn(f"--user {verb} --now {HUB}", self.calls())
            self.assertNotIn(f"--user {verb} {HUB}", self.calls())
        self.assertIn("Voice desk hub not enabled", result.stderr)

    def test_install_sh_runs_the_picker_steps_then_the_desk_reconcile(self):
        self.set_serial("TAB1")
        copy = self.repo
        (copy / "local.env").write_text("DESK_ADB_SERIAL=TAB1\n")
        result = subprocess.run([str(copy / "install.sh")], env=self.env, capture_output=True,
                                text=True, timeout=120, check=False, cwd=copy)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertIn("--user enable --now llm-switchboard.service", calls)
        self.assertIn("--user restart llm-switchboard.service", calls)
        self.assertIn(f"--user enable --now {HUB}", calls)
        self.assertIn(f"--user enable --now {ADB}", calls)
        self.assertIn("Tablet link: enabled and started", result.stdout)
        self.assertTrue((self.home / ".config/systemd/user/llm-switchboard.service").is_file())

    def test_install_sh_writes_nothing_outside_the_fixture(self):
        before = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True,
                                text=True, check=True).stdout
        sentinel = self.root / "sentinel-config"
        (sentinel / "llm-switchboard/desk").mkdir(parents=True)
        (sentinel / "llm-switchboard/desk/mcp.json").write_text("untouched")
        self.test_install_sh_runs_the_picker_steps_then_the_desk_reconcile()
        mcp = Path(self.env["XDG_CONFIG_HOME"]) / "llm-switchboard/desk/mcp.json"
        self.assertTrue(mcp.is_file(), "install-config must land in the fixture XDG root")
        self.assertIn(str(self.repo), mcp.read_text())
        self.assertNotIn(str(REPO), mcp.read_text())
        self.assertEqual((sentinel / "llm-switchboard/desk/mcp.json").read_text(), "untouched")
        self.assertEqual(sorted(p.name for p in sentinel.rglob("*")), ["desk", "llm-switchboard", "mcp.json"])
        after = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True,
                               text=True, check=True).stdout
        self.assertEqual(before, after)
        for path in self.root.rglob("*"):
            self.assertTrue(path.is_relative_to(self.root))

    def test_leaf_under_a_non_default_xdg_config_home_keeps_the_hub_running(self):
        # The leaf exists only under XDG_CONFIG_HOME (setUp), never under HOME/.config.
        self.assertFalse((self.home / ".config/llm-switchboard/desk/tls/leaf.pem").exists())
        self.set_serial("")
        self.desk_units()
        self.clear_calls()
        result = self.desk_units()
        self.assertIn(f"--user restart {HUB}", self.calls())
        self.assertNotIn(f"--user disable --now {HUB}", self.calls())
        self.assertNotIn("not enabled", result.stderr)

    def test_without_xdg_config_home_the_leaf_is_found_under_home(self):
        env = {k: v for k, v in self.env.items() if k != "XDG_CONFIG_HOME"}
        tls = self.home / ".config/llm-switchboard/desk/tls"
        tls.mkdir(parents=True)
        (tls / "leaf.pem").write_text("pem")
        (tls / "leaf.key").write_text("key")
        self.set_serial("")
        self.env = env
        result = self.desk_units()
        self.assertIn(f"--user enable --now {HUB}", self.calls())
        self.assertNotIn("not enabled", result.stderr)

    def test_fixture_repo_has_no_symlinks(self):
        self.assertEqual([p for p in self.repo.rglob("*") if p.is_symlink()], [])

    def test_running_hub_is_stopped_when_its_leaf_goes_missing(self):
        self.set_serial("")
        self.desk_units()
        self.assertTrue((self.fake / "active" / HUB).exists())
        (Path(self.env["XDG_CONFIG_HOME"]) / "llm-switchboard/desk/tls/leaf.key").unlink()
        self.clear_calls()
        self.desk_units()
        self.assertIn(f"--user disable --now {HUB}", self.calls())
        self.assertFalse((self.fake / "active" / HUB).exists())

    def test_active_but_disabled_units_are_enabled(self):
        self.set_serial("TAB1")
        self.desk_units()
        for unit in (HUB, ADB):
            (self.fake / "enabled" / unit).unlink()
        self.clear_calls()
        self.desk_units()
        for unit in (HUB, ADB):
            self.assertTrue((self.fake / "enabled" / unit).exists(), unit)

    def test_unchanged_active_disabled_adb_unit_is_still_enabled(self):
        self.set_serial("TAB1")
        self.desk_units()
        (self.fake / "enabled" / ADB).unlink()
        self.clear_calls()
        self.desk_units()
        self.assertNotIn(f"--user restart {ADB}", self.calls())
        self.assertTrue((self.fake / "enabled" / ADB).exists())

    def test_script_content_change_alone_restarts_adb_unit(self):
        self.set_serial("TAB1")
        self.desk_units()
        link = self.repo / "bin" / "desk-adb-link"
        link.write_text(link.read_text() + "# new behavior\n")
        self.clear_calls()
        result = self.desk_units()
        self.assertIn(f"--user restart {ADB}", self.calls())
        self.assertIn("script", result.stdout)

    def test_failed_render_installs_nothing_and_does_not_reload(self):
        self.add_fake_tool("sed", '#!/bin/bash\n'
                           'case "$*" in *@REPO@*) exit 1;; esac\nexec /usr/bin/sed "$@"\n')
        self.set_serial("TAB1")
        result = self.desk_units()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("--user daemon-reload", self.calls())
        installed = self.home / ".config/systemd/user"
        self.assertEqual([p.name for p in installed.glob("llm-switchboard-desk*")], [])

    def test_failed_replacement_is_an_error_not_unchanged(self):
        self.add_fake_tool("mv", "#!/bin/bash\nexit 1\n")
        self.set_serial("TAB1")
        result = self.desk_units()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("--user daemon-reload", self.calls())
        self.assertNotIn("enable", " ".join(self.calls()))


if __name__ == "__main__":
    unittest.main()
