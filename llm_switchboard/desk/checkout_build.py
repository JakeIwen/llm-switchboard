"""The relay's `relay_build` for the link hello (docs/voice-desk.md,
"Framing": the checkout's git short hash, or '' when it can't be told).

Read from the checkout's git files, never by running git: the relay's
children are only ssh, `ssh -G` and `ssh-keygen` ("Bounds"). Handles a plain
checkout and a worktree (`.git` a `gitdir:` file, refs in the common
directory), a detached HEAD, loose refs and `packed-refs`. Every read is
bounded and never follows a link at the file itself; anything unexpected
gives ''. Standard library only.
"""

from pathlib import Path
import re

from .bounded_read import read_bounded

SHORT_HEX_DIGITS = 12
_SMALL_FILE_BYTES = 4096
_PACKED_REFS_BYTES = 1024 * 1024
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REF = re.compile(r"refs/[A-Za-z0-9._/-]{1,200}")


def checkout_build(repo: Path) -> str:
    """The first 12 hex digits of HEAD's commit in `repo`, or ''."""
    try:
        object_id = _head(Path(repo))
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    return object_id[:SHORT_HEX_DIGITS] if object_id else ""


def _text(path: Path, limit: int = _SMALL_FILE_BYTES) -> str | None:
    data = read_bounded(path, limit)
    return None if data is None else data.decode("ascii").strip()


def _git_dir(repo: Path) -> Path | None:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git
    pointer = _text(dot_git)
    if pointer is None or not pointer.startswith("gitdir: "):
        return None
    return (repo / pointer.removeprefix("gitdir: ")).resolve()


def _head(repo: Path) -> str | None:
    git_dir = _git_dir(repo)
    if git_dir is None:
        return None
    head = _text(git_dir / "HEAD")
    if head is None:
        return None
    if _OBJECT_ID.fullmatch(head):
        return head
    ref = head.removeprefix("ref: ")
    if ref == head or not _REF.fullmatch(ref) or ".." in ref:
        return None
    common = _text(git_dir / "commondir")
    common_dir = git_dir if common is None else (git_dir / common).resolve()
    for directory in dict.fromkeys((git_dir, common_dir)):
        loose = _text(directory / ref)
        if loose is not None:
            return loose if _OBJECT_ID.fullmatch(loose) else None
    return _packed(common_dir, ref)


def _packed(common_dir: Path, ref: str) -> str | None:
    packed = _text(common_dir / "packed-refs", _PACKED_REFS_BYTES)
    for line in (packed or "").splitlines():
        object_id, _, name = line.partition(" ")
        if name == ref and _OBJECT_ID.fullmatch(object_id):
            return object_id
    return None
