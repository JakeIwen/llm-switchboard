"""Dependency boundaries that keep shared records pure and Mac/Pi runtimes separate."""

import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from llm_switchboard.host_launchd import LABEL, SEARCH_LABEL, job, search_job


PACKAGE = Path(__file__).resolve().parents[1] / "llm_switchboard"
REPO = PACKAGE.parent


def internal_imports(module: str) -> set[str]:
    """Return top-level llm_switchboard modules imported by one source file."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                imported.add(node.module.split(".", 1)[0])
            elif node.level == 1 and node.module is None:
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif node.level == 0 and node.module and node.module.startswith("llm_switchboard."):
                imported.add(node.module.split(".", 2)[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("llm_switchboard."):
                    imported.add(alias.name.split(".", 2)[1])
    return imported


class DependencyBoundaryTest(unittest.TestCase):
    def test_shared_records_and_rules_have_only_reviewed_internal_dependencies(self):
        allowed = {
            "host_protocol": {"session_id", "session_names"},
            "host_actions_protocol": {"host_protocol", "session_names"},
            "search_records": set(),
            "session_names": set(),
            "supervisor_state": {"screen"},
        }
        for module, expected in allowed.items():
            with self.subTest(module=module):
                self.assertEqual(internal_imports(module), expected)

    def test_mac_and_pi_host_runtimes_do_not_import_each_other(self):
        mac_modules = {
            "host_agent", "host_launchd", "host_transport", "mac_action_runner",
            "mac_actions", "mac_catalog", "mac_registry", "mac_uploads",
        }
        pi_modules = {
            "host_authorize", "host_exchange", "host_import", "host_queue",
            "host_web", "hosts",
        }
        for module in mac_modules:
            with self.subTest(side="mac", module=module):
                self.assertFalse(internal_imports(module) & pi_modules)
        for module in pi_modules:
            with self.subTest(side="pi", module=module):
                self.assertFalse(internal_imports(module) & mac_modules)


class LaunchContractTest(unittest.TestCase):
    def test_systemd_template_renders_to_the_checkout_wrapper(self):
        template = (REPO / "systemd" / "llm-switchboard.service").read_text(encoding="utf-8")
        rendered = template.replace("@REPO@", str(REPO))
        [exec_start] = [line.removeprefix("ExecStart=") for line in rendered.splitlines()
                        if line.startswith("ExecStart=")]
        self.assertEqual(exec_start, f"{REPO}/bin/llm-switchboard serve")
        self.assertTrue((REPO / "bin" / "llm-switchboard").is_file())
        self.assertTrue(os.access(REPO / "bin" / "llm-switchboard", os.X_OK))

    def test_checkout_wrapper_runs_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, LLM_SWITCHBOARD_CODEX_SOCKET="")
            result = subprocess.run(
                [str(REPO / "bin" / "llm-switchboard"), "--help"], cwd=tmp, env=env,
                capture_output=True, text=True, timeout=10, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Manage Claude Code and Codex sessions", result.stdout)

    def test_launchd_job_runs_the_package_from_the_checkout(self):
        repo = Path("/checkout/llm-switchboard")
        home = Path("/Users/example")
        identity = home / ".ssh" / "switchboard"
        spec = job("pi", "mac", identity, home, "/python3", repo)
        self.assertEqual(spec["Label"], LABEL)
        self.assertEqual(spec["WorkingDirectory"], str(repo))
        self.assertEqual(spec["ProgramArguments"][:4], ["/python3", "-m", "llm_switchboard", "host-agent"])
        self.assertEqual(spec["EnvironmentVariables"]["HOME"], str(home))

    def test_search_index_job_runs_the_indexer_at_background_priority(self):
        repo = Path("/checkout/llm-switchboard")
        home = Path("/Users/example")
        spec = search_job(home, "/python3", repo)
        self.assertEqual(spec["Label"], SEARCH_LABEL)
        self.assertEqual(spec["WorkingDirectory"], str(repo))
        self.assertEqual(spec["ProgramArguments"], ["/python3", "-m", "llm_switchboard", "index", "--quiet"])
        self.assertEqual(spec["EnvironmentVariables"], job("pi", "mac", home, home, "/python3", repo)["EnvironmentVariables"])
        self.assertEqual((spec["Nice"], spec["LowPriorityIO"], spec["ProcessType"]), (19, True, "Background"))
        self.assertLessEqual(spec["StartInterval"], 60)
        self.assertTrue(spec["StandardErrorPath"].startswith(f"{home}/Library/Logs/llm-switchboard/"))


if __name__ == "__main__":
    unittest.main()
