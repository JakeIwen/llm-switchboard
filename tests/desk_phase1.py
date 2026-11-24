"""The phase 1 desk package (commit PHASE1_COMMIT, before the Mac link),
exported with `git archive` into a temporary directory, for the mixed-version
tests: what a hub or CLI that predates the Mac link does with what the new
code writes. Code runs against it in a child interpreter (`-I`), so the two
packages never share a process."""

import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest

REPO = Path(__file__).resolve().parent.parent
PHASE1_COMMIT = "d024217"


def phase1_root(case: unittest.TestCase) -> Path:
    """A directory holding the phase 1 `llm_switchboard` package; SkipTest,
    naming the reason, when the commit isn't in the local repository."""
    found = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{PHASE1_COMMIT}^{{commit}}"],
                           capture_output=True)
    if found.returncode != 0:
        raise unittest.SkipTest(f"phase 1 commit {PHASE1_COMMIT} is not in the local repository")
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", PHASE1_COMMIT, "llm_switchboard"],
                             capture_output=True, check=True)
    root = Path(tempfile.mkdtemp(prefix="desk-phase1"))
    case.addCleanup(shutil.rmtree, root, True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        # Our own repository's tree; the filter where this Python has it.
        tar.extractall(root, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    return root


def run_phase1(root: Path, code: str, *args: str, timeout: float = 60, **kwargs) -> subprocess.CompletedProcess:
    """`code` in a child interpreter that imports only the phase 1 package;
    `sys.argv[1:]` are `args`."""
    prelude = f"import sys; sys.path.insert(0, {str(root)!r})\n"
    return subprocess.run([sys.executable, "-I", "-B", "-c", prelude + code, *args], capture_output=True,
                          text=True, timeout=timeout, **kwargs)


# A phase 1 hub in a child interpreter, for the tests that need one running:
# it prints its HTTPS port (0 when `http` is false, the operator socket only)
# and runs until its stdin closes. HOME and the XDG variables come from the
# environment, as the CLI under test reads them.
_HUB_CHILD = """
import os, sys
from llm_switchboard.desk.config import DeskConfig
from llm_switchboard.desk.enrollment import ensure_private_dir
from llm_switchboard.desk.hub import Hub
from llm_switchboard.tmux import Tmux
config = DeskConfig.from_env()
hub = Hub(config, tmux=Tmux(socket=f"desk-test-none-{os.getpid()}", timeout=5))
if sys.argv[1] == "http":
    hub.start()
    port = hub.http.port
else:
    for directory in (config.config_dir, config.state_dir, config.runtime_dir):
        ensure_private_dir(directory)
    hub.operator.start()
    port = 0
print(port, flush=True)
sys.stdin.read()
hub.close()
"""


def start_phase1_hub(case: unittest.TestCase, root: Path, env: dict, *, http: bool) -> int:
    """Starts the phase 1 hub under `env`; returns its HTTPS port (0 without
    `http`). Stopped at cleanup."""
    prelude = f"import sys; sys.path.insert(0, {str(root)!r})\n"
    child = subprocess.Popen([sys.executable, "-I", "-B", "-c", prelude + _HUB_CHILD, "http" if http else "operator"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env)

    def stop():
        child.stdin.close()
        try:
            child.wait(30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(30)
        child.stdout.close()
        child.stderr.close()
    case.addCleanup(stop)
    # A child that hangs before its port line is killed, so the read ends.
    watchdog = threading.Timer(60, child.kill)
    watchdog.start()
    try:
        line = child.stdout.readline()
    finally:
        watchdog.cancel()
    if not line.strip().isdigit():
        child.kill()
        raise AssertionError(f"phase 1 hub did not start: {line!r} {child.stderr.read()[-2000:]}")
    return int(line)
