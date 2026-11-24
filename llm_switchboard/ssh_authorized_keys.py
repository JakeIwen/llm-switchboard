"""Editing one marked line of `~/.ssh/authorized_keys`, keeping every
unrelated line byte for byte.

A line is ours when its final comment is exactly the marker (v1's
`llm-switchboard-host:<host>`, the desk link's `llm-switchboard-desk-link:<host>`);
a marker appearing anywhere else in someone else's line doesn't count. Every
edit is a whole-file rewrite (temporary file, fsync, rename) made while the
caller holds `locked(path)`, the cross-process flock on `<path>.lock` that
`JsonFile.locked` takes, so a command that must keep the file and another
one consistent can hold this lock across both of its edits.
Standard library only (the SSH forced commands import it).
"""

import contextlib
import os
from pathlib import Path
import tempfile

from .json_file import JsonFile


def authorized_keys_path(home: Path) -> Path:
    """`<home>/.ssh/authorized_keys`, creating `.ssh` 0700 when missing.
    Refuses a symlinked file: the rename would replace the link, not the
    file it names."""
    ssh = Path(home) / ".ssh"
    ssh.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = ssh / "authorized_keys"
    if path.is_symlink():
        raise ValueError("authorized_keys must not be a symlink")
    return path


@contextlib.contextmanager
def locked(path: Path):
    """The file's cross-process lock. Not re-entrant: take it once per edit
    sequence and call the edits inside it."""
    with JsonFile(Path(path)).locked():
        yield


def has_marker(line: bytes, marker: bytes) -> bool:
    return line.rstrip(b"\r\n").endswith(b" " + marker)


def read_lines(path: Path) -> list[bytes]:
    """The file's lines with their endings; a missing file has none."""
    path = Path(path)
    return path.read_bytes().splitlines(keepends=True) if path.exists() else []


def replace_marked_line(path: Path, marker: bytes, line: bytes) -> bool:
    """Write `line` (ending in its marker and a newline) in place of every
    line carrying `marker`, after the other lines. The caller holds
    `locked(path)`. True when the content changed."""
    if not line.endswith(b"\n") or not has_marker(line, marker):
        raise ValueError("the new line must end with its marker and a newline")
    return _rewrite(Path(path), marker, line)


def remove_marked_line(path: Path, marker: bytes) -> bool:
    """Drop every line carrying `marker`. The caller holds `locked(path)`.
    True when the content changed."""
    return _rewrite(Path(path), marker, b"")


def _rewrite(path: Path, marker: bytes, new: bytes) -> bool:
    if not new and not path.exists():
        return False
    lines = read_lines(path)
    original = b"".join(lines)
    content = b"".join(line for line in lines if not has_marker(line, marker))
    if content and not content.endswith(b"\n"):
        content += b"\n"
    content += new
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".authorized_keys-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return content != original
