"""Install/unload the Mac's user launchd jobs, without sudo or a web server."""

import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

LABEL = "dev.llm-switchboard.host-agent"
# The Mac has no `serve`, so launchd runs the transcript indexer that serve's child runs on the Pi.
SEARCH_LABEL = "dev.llm-switchboard.search-index"
SEARCH_INTERVAL_S = 30
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _logs(home: Path) -> Path:
    return home / "Library" / "Logs" / "llm-switchboard"


def job(pi: str, host: str, identity: Path, home: Path, python: str, repo: Path) -> dict:
    logs = _logs(home)
    return {"Label": LABEL, "ProgramArguments": [python, "-m", "llm_switchboard", "host-agent",
            "--pi", pi, "--host-name", host, "--identity", str(identity)],
            "WorkingDirectory": str(repo), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "EnvironmentVariables": {"HOME": str(home), "PATH": PATH},
            "StandardOutPath": str(logs / "agent.log"), "StandardErrorPath": str(logs / "agent-error.log")}


def search_job(home: Path, python: str, repo: Path) -> dict:
    """A short `index --quiet` run every 30 s at background CPU and I/O priority."""
    logs = _logs(home)
    return {"Label": SEARCH_LABEL, "ProgramArguments": [python, "-m", "llm_switchboard", "index", "--quiet"],
            "WorkingDirectory": str(repo), "RunAtLoad": True, "StartInterval": SEARCH_INTERVAL_S,
            "Nice": 19, "LowPriorityIO": True, "ProcessType": "Background",
            "EnvironmentVariables": {"HOME": str(home), "PATH": PATH},
            "StandardOutPath": str(logs / "search-index.log"),
            "StandardErrorPath": str(logs / "search-index-error.log")}


def install(pi: str, host: str, identity: Path, *, unload=False, home=None, run=subprocess.run) -> Path:
    if sys.platform != "darwin":
        raise ValueError("host-install must run on the Mac")
    home = Path(home or Path.home())
    agents = home / "Library" / "LaunchAgents"
    path = agents / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if unload:
        # Installs from before the search job have none to unload.
        run(["launchctl", "bootout", f"{domain}/{SEARCH_LABEL}"], check=False, capture_output=True)
        run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=True)
        return path
    agents.mkdir(parents=True, exist_ok=True)
    _logs(home).mkdir(parents=True, exist_ok=True)
    if not identity.is_file():
        raise ValueError(f"dedicated private key not found: {identity}")
    repo = Path(__file__).resolve().parents[1]
    jobs = {LABEL: job(pi, host, identity, home, sys.executable, repo),
            SEARCH_LABEL: search_job(home, sys.executable, repo)}
    for label, spec in jobs.items():
        plist = agents / f"{label}.plist"
        plist.write_bytes(plistlib.dumps(spec))
        run(["launchctl", "bootout", f"{domain}/{label}"], check=False, capture_output=True)
        _bootstrap(run, domain, plist)
    return path


def _bootstrap(run, domain: str, plist: Path, attempts: int = 10) -> None:
    # bootout returns before launchd has removed a running job; bootstrapping
    # it again meanwhile fails with "5: Input/output error".
    for _ in range(attempts):
        result = run(["launchctl", "bootstrap", domain, str(plist)], check=False, capture_output=True)
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
