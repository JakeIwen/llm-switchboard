"""Install/unload the Mac's user launchd publisher, without sudo or a web server."""

import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = "dev.llm-switchboard.host-agent"


def job(pi: str, host: str, identity: Path, home: Path, python: str, repo: Path) -> dict:
    logs = home / "Library" / "Logs" / "llm-switchboard"
    return {"Label": LABEL, "ProgramArguments": [python, "-m", "llm_switchboard", "host-agent",
            "--pi", pi, "--host-name", host, "--identity", str(identity)],
            "WorkingDirectory": str(repo), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "EnvironmentVariables": {"HOME": str(home), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
            "StandardOutPath": str(logs / "agent.log"), "StandardErrorPath": str(logs / "agent-error.log")}


def install(pi: str, host: str, identity: Path, *, unload=False, home=None, run=subprocess.run) -> Path:
    if sys.platform != "darwin":
        raise ValueError("host-install must run on the Mac")
    home = Path(home or Path.home())
    path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if unload:
        run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    (home / "Library" / "Logs" / "llm-switchboard").mkdir(parents=True, exist_ok=True)
    if not identity.is_file():
        raise ValueError(f"dedicated private key not found: {identity}")
    path.write_bytes(plistlib.dumps(job(pi, host, identity, home, sys.executable, Path(__file__).resolve().parents[1])))
    run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False, capture_output=True)
    run(["launchctl", "bootstrap", domain, str(path)], check=True)
    return path
