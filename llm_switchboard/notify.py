"""Push notifications through an external script, ~/scripts/ntfy_send.sh by
default, called as: <script> <title> <message> <priority> <tags>.

LLM_SWITCHBOARD_NOTIFY points at a different script; set it empty to turn
notifications off. Sending happens in a background thread, so a slow or
failing notifier never holds up the supervisor.
"""

from pathlib import Path
import logging
import os
import subprocess
import threading

from .paths import HOME

log = logging.getLogger(__name__)

DEFAULT_SCRIPT = HOME / "scripts" / "ntfy_send.sh"


def default_script() -> Path | None:
    configured = os.environ.get("LLM_SWITCHBOARD_NOTIFY")
    if configured is not None:
        return Path(configured) if configured else None
    return DEFAULT_SCRIPT if DEFAULT_SCRIPT.is_file() else None


class Notifier:
    def __init__(self, script: Path | None = None):
        self.script = script

    @property
    def enabled(self) -> bool:
        return self.script is not None

    def send(self, title: str, message: str, priority: str = "default", tags: str = "") -> None:
        if not self.script:
            return
        argv = ["bash", str(self.script), title, message, priority, tags]
        threading.Thread(target=self._run, args=(argv,), daemon=True, name="notify").start()

    def _run(self, argv: list[str]) -> None:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("notification failed: %s", e)
            return
        if result.returncode != 0:
            log.warning("notification failed (%s): %s", result.returncode, result.stderr.strip())
