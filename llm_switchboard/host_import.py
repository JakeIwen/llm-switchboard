"""Explicit, bounded Claude transcript imports; never keep or launch the copy."""

import base64
import json
import os
from pathlib import Path
import tempfile
import uuid

from .codex_client import CodexClient
from .codex_threads import CodexCatalog, codex_names
from .folders import existing_folder
from .host_actions_protocol import MAX_UPLOAD_BYTES
from .host_protocol import folder
from .names import Names, claude_names
from .paths import HOME, PROJECTS_DIR
from .relocate import fork, project_dir
from .store import Store
from .transcripts import TranscriptIndex


def mapped_folder(cwd: str, pi_home: Path = HOME) -> str:
    source = Path(folder(cwd))
    try:
        rest = source.relative_to("/Users/jacobr")
    except ValueError as e:
        raise ValueError("Fork onto Pi only maps /Users/jacobr/<folder> to the Pi home folder.") from e
    if not rest.parts:
        raise ValueError("Choose a session in a working folder, not the Mac home folder.")
    target = Path(pi_home) / rest
    if not target.is_dir():
        raise ValueError(f"Mapped folder does not exist on the Pi: {target}")
    if not target.resolve().is_relative_to(Path(pi_home).resolve()):
        raise ValueError("Mapped folder resolves outside the Pi home folder.")
    return existing_folder(str(target))


class HostImporter:
    def __init__(self, projects=PROJECTS_DIR, pi_home=HOME, names=None):
        self.projects, self.pi_home = Path(projects), Path(pi_home)
        self.names = names or Names(claude_names(TranscriptIndex(self.projects), Store()),
                                    codex_names(CodexCatalog(CodexClient())))

    def check(self, cwd: str, title: str) -> str:
        target = mapped_folder(cwd, self.pi_home)
        self.names.refuse_duplicate(target, title)
        return target

    def receive(self, item: dict, upload: dict) -> str:
        cwd, title = self.check(item["cwd"], item["body"]["title"]), item["body"]["title"]
        encoded = upload.get("transcript")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
            raise ValueError("transcript upload exceeds 16 MiB")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as e:
            raise ValueError("invalid transcript encoding") from e
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("transcript upload exceeds 16 MiB")
        normalized = normalize_transcript(content, item["session_id"])
        new_id = str(uuid.uuid4())
        target_dir = project_dir(self.projects, cwd)
        target_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target_dir, prefix=".host-import-") as tmp:
            stage = Path(tmp)
            source = stage / "upload.jsonl"
            source.write_bytes(normalized)
            copy = fork(source, item["session_id"], new_id, item["cwd"], cwd, title, stage / "projects")
            # A hard-link publishes exclusively: unlike replace(), it cannot overwrite.
            os.link(copy, target_dir / f"{new_id}.jsonl")
        return f"Forked onto the Pi as {new_id}; not kept running."


def normalize_transcript(content: bytes, sid: str) -> bytes:
    records = []
    for line in content.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue  # Match relocate.fork: discard an unfinished final record.
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError) as e:
            raise ValueError("transcript must contain complete JSON records") from e
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            raise ValueError("invalid transcript record")
        if "sessionId" in record and record["sessionId"] != sid:
            raise ValueError("transcript contains another session id")
        records.append(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
    if not records:
        raise ValueError("transcript is empty")
    return b"".join(records)
