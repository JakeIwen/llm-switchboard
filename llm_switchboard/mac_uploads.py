"""The one transcript export path: explicit notification, audit log and durable quota."""

import base64
from pathlib import Path
import subprocess
import time

from .host_actions_protocol import MAX_UPLOAD_BYTES
from .json_file import JsonFile
from .paths import HOME, STATE_DIR

NOTIFY_SCRIPT = '''on run argv
  display notification (item 1 of argv) with title "LLM Switchboard"
end run'''


class MacUploads:
    def __init__(self, state=STATE_DIR / "uploads.json", logs=HOME / "Library/Logs/llm-switchboard",
                 clock=time.time, run=subprocess.run):
        self.file, self.logs, self.clock, self.run = JsonFile(state), Path(logs), clock, run

    def export(self, transcript, action_id: str) -> dict:
        if transcript.path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("Transcript is larger than the 16 MiB upload limit.")
        with self.file.locked():
            now = self.clock()
            recent = [t for t in self.file.load().get("uploads", []) if t > now - 3600]
            if len(recent) >= 10:
                raise ValueError("Mac transcript upload limit reached (10 per hour). Try again later.")
            recent.append(now)
            self.file.write({"uploads": recent})
        self.run(["osascript", "-e", NOTIFY_SCRIPT, "Uploading one Claude session to the Pi: " + transcript.session_id],
                 check=True, capture_output=True, timeout=10)
        self.logs.mkdir(parents=True, exist_ok=True)
        with open(self.logs / "uploads.log", "a") as log:
            log.write(f"{now:.3f} action={action_id} session={transcript.session_id} upload-attempt\n")
        with open(transcript.path, "rb") as f:
            content = f.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("Transcript grew beyond the 16 MiB upload limit.")
        return {"action_id": action_id, "transcript": base64.b64encode(content).decode("ascii")}
