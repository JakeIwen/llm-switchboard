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
