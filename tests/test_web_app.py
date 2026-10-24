"""Runs the new page's checks in web/: typecheck, unit tests, the react-app CSS
and structure checks, and a production build into a scratch folder (never
web/dist, which the service serves)."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def npm(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["npm", *args], cwd=WEB, capture_output=True, text=True, timeout=timeout)


class WebAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("npm"):
            raise AssertionError("npm is required to check web/")
        if not (WEB / "node_modules").is_dir():
            raise AssertionError("web/node_modules is missing: run `npm ci` in web/")

    def run_ok(self, *args: str) -> None:
        result = npm(*args)
        self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-4000:])

    def test_checks_and_unit_tests(self):
        for script in ("check:css", "check:structure", "typecheck", "test"):
            with self.subTest(script=script):
                self.run_ok("run", "--silent", script)

    def test_production_build(self):
        with tempfile.TemporaryDirectory() as out:
            self.run_ok("exec", "--", "vite", "build", "--outDir", out, "--emptyOutDir", "--logLevel", "error")
            self.assertTrue((Path(out) / "index.html").is_file())


class GeneratedTypesTest(unittest.TestCase):
    """web/openapi.json and web/src/api/schema.d.ts must match the server: run
    `npm run gen:api` in web/ after changing a route or record."""

    def test_schema_and_types_match_the_server(self):
        exported = subprocess.run([sys.executable, "-m", "llm_switchboard", "openapi"], cwd=WEB.parent,
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(exported.stdout, (WEB / "openapi.json").read_text(), "web/openapi.json is stale: npm run gen:api")
        with tempfile.TemporaryDirectory() as out:
            types = Path(out) / "schema.d.ts"
            result = npm("exec", "--", "openapi-typescript", "openapi.json", "-o", str(types),
                         "--default-non-nullable", "false")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(types.read_text(), (WEB / "src/api/schema.d.ts").read_text(),
                             "web/src/api/schema.d.ts is stale: npm run gen:api")
