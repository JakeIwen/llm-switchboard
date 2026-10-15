"""Moving a conversation to another working folder.

Claude Code files each transcript under projects/<encoded cwd>/ and resumes
`--resume <id>` from the folder matching the current directory, so a session
belongs to a folder by where its file lives. Moving one means putting the
file (and its <id>/ companion folder) under the new folder's name, and
pointing the transcript's "cwd" fields that named the old folder at the new
one so the history agrees with where the session now runs.
"""

from pathlib import Path
import json
import os
import re
import shutil
import tempfile

# Claude Code shortens longer names with a hash; those aren't handled here.
MAX_DIR_NAME = 200


def project_dir_name(cwd: str) -> str:
    """Claude Code's folder name for a working directory: every character
    other than a letter or digit becomes "-"."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def project_dir(projects_dir: Path, cwd: str) -> Path:
    name = project_dir_name(cwd)
    if len(name) > MAX_DIR_NAME:
        raise ValueError("folder path is too long")
    return Path(projects_dir) / name


def cwd_field(cwd: str) -> bytes:
    """A transcript's "cwd" field exactly as Claude Code writes it."""
    return b'"cwd":' + json.dumps(cwd, ensure_ascii=False).encode()


def write_rewritten(src: Path, target: Path, rewrite, keep_stat: bool) -> None:
    """Atomically write src's lines to target, each passed through rewrite
    (which returns the bytes to write, or None to leave the line out)."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            for line in f:
                line = rewrite(line)
                if line is not None:
                    out.write(line)
            out.flush()
            os.fsync(out.fileno())
        if keep_stat:
            shutil.copystat(src, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def relocate(transcript: Path, session_id: str, old_cwd: str, new_cwd: str, projects_dir: Path) -> Path:
    """Move the transcript to new_cwd's project folder and return its new path."""
    target_dir = project_dir(projects_dir, new_cwd)
    target = target_dir / f"{session_id}.jsonl"
    companion, target_companion = transcript.with_suffix(""), target.with_suffix("")
    if target.exists() and target != transcript:
        raise FileExistsError(f"{target} already exists")
    target_dir.mkdir(parents=True, exist_ok=True)

    # Replace the exact JSON value, so every other byte of the transcript is
    # left as Claude Code wrote it.
    old_field, new_field = cwd_field(old_cwd), cwd_field(new_cwd)
    write_rewritten(transcript, target, lambda line: line.replace(old_field, new_field) if old_cwd else line,
                    keep_stat=True)
    if target != transcript:
        transcript.unlink()
        if companion.is_dir() and not target_companion.exists():
            shutil.move(str(companion), target_companion)
    return target
