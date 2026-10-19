"""Run transcript indexing periodically in a low-priority child process."""

import logging
import shutil
import subprocess
import sys
import threading

log = logging.getLogger(__name__)


class SearchIndexScheduler:
    def __init__(self, interval_s: float = 30.0):
        self.interval_s = interval_s
        self._process: subprocess.Popen | None = None

    def start(self, stop: threading.Event) -> threading.Thread:
        worker = threading.Thread(target=self._run, args=(stop,), name="search-index-scheduler", daemon=True)
        worker.start()
        return worker

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self._launch()
            if stop.wait(self.interval_s):
                break
        if self._process and self._process.poll() is None:
            # The child owns no live-session state; let its current file transaction finish.
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.terminate()

    def _launch(self) -> None:
        if self._process and self._process.poll() is None:
            return
        if self._process and self._process.returncode:
            log.warning("transcript indexer exited with status %s", self._process.returncode)
        command = [sys.executable, "-m", "llm_switchboard", "index"]
        nice = shutil.which("nice")
        if nice:
            command = [nice, "-n", "19", *command]
        ionice = shutil.which("ionice")
        if ionice:
            command = [ionice, "-c", "3", *command]
        self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
