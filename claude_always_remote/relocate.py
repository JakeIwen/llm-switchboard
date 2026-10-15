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


def relocate(transcript: Path, session_id: str, old_cwd: str, new_cwd: str, projects_dir: Path) -> Path:
    """Move the transcript to new_cwd's project folder and return its new path."""
    name = project_dir_name(new_cwd)
    if len(name) > MAX_DIR_NAME:
        raise ValueError("folder path is too long")
    target_dir = Path(projects_dir) / name
    target = target_dir / f"{session_id}.jsonl"
    companion, target_companion = transcript.with_suffix(""), target.with_suffix("")
    if target.exists() and target != transcript:
        raise FileExistsError(f"{target} already exists")
    target_dir.mkdir(parents=True, exist_ok=True)

    # Replace the exact JSON value, so every other byte of the transcript is
    # left as Claude Code wrote it.
    old_field = b'"cwd":' + json.dumps(old_cwd, ensure_ascii=False).encode()
    new_field = b'"cwd":' + json.dumps(new_cwd, ensure_ascii=False).encode()
    fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=f".{session_id}-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "wb") as out, open(transcript, "rb") as src:
            for line in src:
                out.write(line.replace(old_field, new_field) if old_cwd else line)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(transcript, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if target != transcript:
        transcript.unlink()
        if companion.is_dir() and not target_companion.exists():
            shutil.move(str(companion), target_companion)
    return target
