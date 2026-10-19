"""Locked, atomic JSON files shared by short-lived commands and the service."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile


class JsonFile:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    @contextlib.contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def write(self, payload: dict) -> None:
        """Caller holds locked() when participating in a read/modify/write."""
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.stem}-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
