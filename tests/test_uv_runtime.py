"""Exercise checkout launch and installation contracts without changing a service."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class UvRuntimeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "checkout"
        (self.repo / "bin").mkdir(parents=True)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.tools}:/usr/bin:/bin", "PYTHONPATH": "existing"}

    def executable(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + content + "\n")
        path.chmod(0o700)

    def test_wrapper_prefers_checkout_venv_and_preserves_pythonpath(self):
        wrapper = self.repo / "bin/llm-switchboard"
        shutil.copy2(REPO / "bin/llm-switchboard", wrapper)
        self.executable(self.tools / "python3", 'printf "fallback|%s|%s\\n" "$PYTHONPATH" "$*"')
        command = [str(wrapper), "settings"]
        fallback = subprocess.run(command, env=self.env, cwd=self.home, capture_output=True, text=True, check=True)
        self.assertEqual(fallback.stdout.strip(), f"fallback|{self.repo.resolve()}:existing|-m llm_switchboard settings")
        self.executable(self.repo / ".venv/bin/python", 'printf "venv|%s|%s\\n" "$PYTHONPATH" "$*"')
        managed = subprocess.run(command, env=self.env, cwd=self.home, capture_output=True, text=True, check=True)
        self.assertEqual(managed.stdout.strip(), f"venv|{self.repo.resolve()}:existing|-m llm_switchboard settings")

    def test_install_requires_uv_before_writing_service_files(self):
        shutil.copy2(REPO / "install.sh", self.repo / "install.sh")
        result = subprocess.run(["/bin/bash", str(self.repo / "install.sh")], env=self.env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uv is required", result.stderr)
        self.assertFalse((self.home / ".config").exists())

    def test_install_frozen_sync_uses_system_python_and_stops_on_failure(self):
        shutil.copy2(REPO / "install.sh", self.repo / "install.sh")
        self.executable(self.tools / "uv", 'printf "%s|%s|%s|%s\\n" "$PWD" "$UV_PYTHON_PREFERENCE" "$UV_PYTHON_DOWNLOADS" "$*"; exit 17')
        result = subprocess.run(["/bin/bash", str(self.repo / "install.sh")], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(result.stdout.strip(), f"{self.repo}|only-system|never|sync --frozen")
        self.assertFalse((self.home / ".config").exists())
