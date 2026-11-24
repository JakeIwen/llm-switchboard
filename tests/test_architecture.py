"""Dependency boundaries that keep shared records pure and Mac/Pi runtimes separate."""

import ast
import os
import subprocess
import tempfile
import unittest
from importlib.util import resolve_name
from pathlib import Path

from llm_switchboard.host_launchd import HOST_SEARCH_LABEL, LABEL, SEARCH_LABEL, host_search_job, job, search_job


PACKAGE = Path(__file__).resolve().parents[1] / "llm_switchboard"
REPO = PACKAGE.parent


DESK_MAC = {
    "checkout_build", "mac_machine", "mac_process", "peer_mac", "rate_log",
    "relay", "relay_channels", "relay_cli", "relay_launchd", "relay_link", "relay_ssh",
}
DESK_PI = {
    "enrollment_views", "grants", "http_server", "hub", "hub_enrollment", "hub_link_ops",
    "hub_state", "labels", "link_admission", "link_authorize", "link_candidates", "link_fence",
    "link_hub", "link_pump", "link_session", "link_views", "locks", "operator", "page_build",
    "peer", "remote_enrollment", "startup_ack", "startup_identity", "startup_registry", "text_rule",
}
DESK_SHARED = {
    "__init__", "bounded_read", "channel", "child_process", "cli", "config", "enrollment",
    "link_proof", "link_protocol", "link_writer", "model", "peer_credentials", "private_socket",
    "protocol", "refusals", "run_lock", "session_identity", "shallow_json",
}
DESK_PREFIX = "llm_switchboard.desk."


def source_imports(module: str) -> set[str]:
    """Return qualified import targets for a source file, including lazy imports."""
    source = PACKAGE.joinpath(*module.split(".")).with_suffix(".py")
    package = f"llm_switchboard.{module}".rsplit(".", 1)[0]
    imported = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            origin = resolve_name("." * node.level + (node.module or ""), package)
            imported.update(f"{origin}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def internal_imports(module: str) -> set[str]:
    """Return top-level llm_switchboard modules imported by one source file."""
    return {name.split(".", 2)[1] for name in source_imports(module)
            if name.startswith("llm_switchboard.")}


def desk_imports(module: str) -> set[str]:
    return {name.removeprefix(DESK_PREFIX) for name in source_imports(f"desk.{module}")
            if name.startswith(DESK_PREFIX)}


class DependencyBoundaryTest(unittest.TestCase):
    def test_shared_records_and_rules_have_only_reviewed_internal_dependencies(self):
        allowed = {
            "host_protocol": {"session_id", "session_names"},
            "host_actions_protocol": {"host_protocol", "session_names"},
            "search_records": set(),
            "session_names": set(),
            "ssh_public_key": set(),
            "supervisor_state": {"screen"},
        }
        for module, expected in allowed.items():
            with self.subTest(module=module):
                self.assertEqual(internal_imports(module), expected)

    def test_mac_and_pi_host_runtimes_do_not_import_each_other(self):
        mac_modules = {
            "host_agent", "host_launchd", "host_transport", "mac_action_runner",
            "mac_actions", "mac_catalog", "mac_registry", "mac_uploads", "mac_search_server",
        }
        pi_modules = {
            "host_authorize", "host_exchange", "host_import", "host_queue",
            "host_web", "hosts", "ssh_authorized_keys",
        }
        for module in mac_modules:
            with self.subTest(side="mac", module=module):
                self.assertFalse(internal_imports(module) & pi_modules)
        for module in pi_modules:
            with self.subTest(side="pi", module=module):
                self.assertFalse(internal_imports(module) & mac_modules)


class DeskDependencyBoundaryTest(unittest.TestCase):
    def test_every_desk_module_has_one_side(self):
        modules = {source.stem for source in (PACKAGE / "desk").glob("*.py")}
        self.assertSetEqual(modules, DESK_MAC | DESK_PI | DESK_SHARED)
        self.assertFalse((DESK_MAC & DESK_PI) | (DESK_MAC & DESK_SHARED) | (DESK_PI & DESK_SHARED))
        self.assertFalse({module for module in modules if module.startswith("relay")} - DESK_MAC)

    def test_pi_desk_and_channel_do_not_import_mac_desk(self):
        for module in sorted(DESK_PI | {"channel"}):
            with self.subTest(module=module):
                self.assertFalse({name for name in desk_imports(module)
                                  if name.split(".", 1)[0] in DESK_MAC})

    def test_shared_cli_reaches_mac_desk_only_through_relay_cli(self):
        self.assertFalse({name for name in desk_imports("cli")
                          if name.split(".", 1)[0] in DESK_MAC - {"relay_cli"}})

    def test_mac_desk_and_channel_do_not_import_pi_desk(self):
        for module in sorted(DESK_MAC | {"channel"}):
            with self.subTest(module=module):
                self.assertFalse({name for name in desk_imports(module)
                                  if name.split(".", 1)[0] in DESK_PI})

    def test_desk_runtimes_do_not_import_other_hosts_v1_runtime(self):
        for module in sorted(DESK_PI):
            with self.subTest(side="pi", module=module):
                self.assertFalse({name for name in internal_imports(f"desk.{module}")
                                  if name.startswith("mac_") or name in {"host_agent", "host_launchd", "host_transport"}})
        for module in sorted(DESK_MAC):
            with self.subTest(side="mac", module=module):
                self.assertFalse({name for name in internal_imports(f"desk.{module}")
                                  if name.startswith("web") or name in {
                                      "host_authorize", "host_exchange", "host_import", "host_queue",
                                      "host_web", "hosts", "http", "ssh_authorized_keys",
                                  }})

    def test_desk_does_not_import_the_http_stack(self):
        external = {"fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core"}
        internal = {"host_web", "http", "web", "web_app"}
        for module in sorted(DESK_MAC | DESK_PI | DESK_SHARED):
            with self.subTest(module=module):
                self.assertFalse({name.split(".", 1)[0] for name in source_imports(f"desk.{module}")} & external)
                self.assertFalse(internal_imports(f"desk.{module}") & internal)


class LaunchContractTest(unittest.TestCase):
    def test_systemd_template_renders_to_the_checkout_wrapper(self):
        template = (REPO / "systemd" / "llm-switchboard.service").read_text(encoding="utf-8")
        rendered = template.replace("@REPO@", str(REPO))
        [exec_start] = [line.removeprefix("ExecStart=") for line in rendered.splitlines()
                        if line.startswith("ExecStart=")]
        self.assertEqual(exec_start, f"{REPO}/bin/llm-switchboard serve")
        self.assertTrue((REPO / "bin" / "llm-switchboard").is_file())
        self.assertTrue(os.access(REPO / "bin" / "llm-switchboard", os.X_OK))

    def test_desk_units_render_to_executable_checkout_targets(self):
        cases = {
            "llm-switchboard-desk.service": ("bin/llm-switchboard", "desk serve"),
            "llm-switchboard-desk-adb.service": ("bin/desk-adb-link", "${DESK_ADB_SERIAL} 8796"),
        }
        for unit, (target, args) in cases.items():
            with self.subTest(unit=unit):
                rendered = (REPO / "systemd" / unit).read_text(encoding="utf-8").replace("@REPO@", str(REPO))
                self.assertNotIn("@REPO@", rendered)
                [exec_start] = [line.removeprefix("ExecStart=") for line in rendered.splitlines()
                                if line.startswith("ExecStart=")]
                self.assertEqual(exec_start, f"{REPO}/{target} {args}")
                self.assertTrue(os.access(REPO / target, os.X_OK))
        adb_unit = (REPO / "systemd" / "llm-switchboard-desk-adb.service").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=@REPO@/local.env", adb_unit)
        self.assertIn("RestartPreventExitStatus=2 4", adb_unit)

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

    def test_host_search_job_runs_the_loopback_search_server(self):
        repo = Path("/checkout/llm-switchboard")
        home = Path("/Users/example")
        spec = host_search_job("mac", home, "/python3", repo)
        self.assertEqual(spec["Label"], HOST_SEARCH_LABEL)
        self.assertEqual(spec["WorkingDirectory"], str(repo))
        self.assertEqual(spec["ProgramArguments"],
                         ["/python3", "-m", "llm_switchboard", "host-search", "--host-name", "mac"])
        self.assertEqual(spec["EnvironmentVariables"], job("pi", "mac", home, home, "/python3", repo)["EnvironmentVariables"])
        self.assertEqual((spec["RunAtLoad"], spec["KeepAlive"], spec["ThrottleInterval"]), (True, True, 10))
        for field in ("StandardOutPath", "StandardErrorPath"):
            self.assertTrue(spec[field].startswith(f"{home}/Library/Logs/llm-switchboard/"))

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
