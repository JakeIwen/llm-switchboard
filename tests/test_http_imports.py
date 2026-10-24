"""The SSH/Mac/CLI entry points remain usable without the HTTP environment."""

import base64
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
HEAVY = {"fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core"}
# -I removes the checkout; this single test bootstrap supplies only its explicit
# root, not a user site or a PYTHONPATH inherited from an interactive session.
BOOTSTRAP = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('llm_switchboard',run_name='__main__')"


class LightImportsTest(unittest.TestCase):
    def test_each_light_entry_point_exercises_its_runtime_without_http_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / "bin"
            tools.mkdir()
            for name, output in [("ssh", '{"version":1,"actions":[]}'), ("launchctl", "")]:
                script = tools / name
                script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
                script.chmod(0o700)
            key = root / "identity"
            key.write_text("fixture key, never sent")
            fields = [b"ssh-ed25519", b"x" * 32]
            pubkey = "ssh-ed25519 " + base64.b64encode(b"".join(struct.pack(">I", len(f)) + f for f in fields)).decode()
            env = {**os.environ, "HOME": str(root), "XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "",
                   "LLM_SWITCHBOARD_CODEX_SOCKET": "", "LLM_SWITCHBOARD_NOTIFY": "",
                   "PATH": str(tools) + os.pathsep + os.environ["PATH"]}
            cases = [
                (["host-exchange", "mac"], '{"version":1,"rows":[]}', 0, "host_exchange"),
                (["host-authorize", "mac"], pubkey, 0, "host_authorize"),
                (["host-agent", "--once", "--identity", str(key)], "", 0, "host_agent"),
                (["host-install", "--identity", str(key)], "", 0 if sys.platform == "darwin" else 1, "host_launchd"),
                (["search", "--json", "amber"], "", 3, "search_cli"),
                (["index"], "", 0, "search_indexer"),
                (["settings"], "", 0, "settings_cli"),
            ]
            for args, stdin, code, module in cases:
                with self.subTest(command=args[0]):
                    result = subprocess.run([sys.executable, "-I", "-X", "importtime", "-c", BOOTSTRAP,
                                             str(REPO), *args], cwd=root, env=env, input=stdin,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, code, result.stderr[-2000:])
                    imports = {line.rsplit("|", 1)[-1].strip() for line in result.stderr.splitlines()
                               if line.startswith("import time:")}
                    self.assertIn("llm_switchboard." + module, imports)
                    self.assertFalse({name.split(".")[0] for name in imports} & HEAVY, imports)
                    if args[0] == "host-agent":
                        self.assertIn("llm_switchboard.mac_action_runner", imports)
                        self.assertIn("llm_switchboard.mac_uploads", imports)
