"""Rename, delete and restore sessions without fighting a running claude process.

A running process keeps its own idea of the title and keeps appending to its
transcript, so:
- rename goes through `/rename` typed into the session when we run it, is
  appended as a custom-title record when nothing has it open, and is refused
  when another claude process has it open;
- delete stops a kept-running session first, is refused while another claude
  process has it open, and moves the files to the trash so it can be undone;
- move (to another working folder) stops a kept-running session, moves its
  files, and lets the supervisor relaunch it in the new folder.
"""

import json
import os
import time
import uuid

from .folders import new_session_folders
from .live_registry import live_processes
from .relocate import relocate
from .screen import blocking_prompt
from .session_id import short_id
from .store import Store
from .supervisor import Supervisor
from .tmux import Tmux
from .transcripts import TranscriptIndex
from .trash import Trash

MAX_TITLE_LEN = 120
_STOP_TIMEOUT_S = 20


class Conflict(Exception):
    """The session's current state doesn't allow this right now."""


class SessionActions:
    def __init__(self, store: Store, index: TranscriptIndex, supervisor: Supervisor, tmux: Tmux, trash: Trash,
                 registry=live_processes, sleep=time.sleep, folders=new_session_folders):
        self.store = store
        self.index = index
        self.supervisor = supervisor
        self.tmux = tmux
        self.trash = trash
        self.registry = registry
        self.sleep = sleep
        self.folders = folders

    @staticmethod
    def _clean_title(title) -> str:
        if not isinstance(title, str) or not " ".join(title.split()):
            raise ValueError("a name is required")
        title = " ".join(title.split())
        if len(title) > MAX_TITLE_LEN:
            raise ValueError(f"the name must be at most {MAX_TITLE_LEN} characters")
        return title

    def rename(self, sid: str, title) -> dict:
        title = self._clean_title(title)
        transcript = self._transcript(sid)
        self._refuse_if_open_elsewhere(sid, "rename")
        window = self._window(sid)
        if window is None:
            record = {"type": "custom-title", "customTitle": title, "sessionId": sid}
            with open(transcript.path, "a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
            return {"ok": True, "title": title, "via": "transcript"}
        ours = next((p for p in self.registry() if p.managed_for == sid), None)
        if ours is None or ours.status != "idle" or blocking_prompt(self.tmux.capture(window.window_id)):
            raise Conflict("The session is starting, working or waiting on a prompt; rename it when it's idle.")
        self.tmux.send_line(window.window_id, f"/rename {title}")
        return {"ok": True, "title": title, "via": "running session"}

    def create(self, cwd, title) -> dict:
        """Start a brand-new session in one of the offered folders, kept running."""
        title = self._clean_title(title)
        folder = next((f for f in self.folders(self.index.all()) if f.cwd == cwd), None)
        if folder is None:
            raise ValueError("pick one of the listed folders")
        if folder.blocked:
            raise Conflict(folder.blocked)
        sid = str(uuid.uuid4())
        self.store.create(sid, cwd, title)
        self.supervisor.announce_when_ready(sid)
        return {"ok": True, "id": sid, "title": title}

    def delete(self, sid: str) -> dict:
        entry = self.store.load().get(sid)
        if self.index.get(sid) is None and entry and entry.cwd:
            # Created here and never written to disk: just stop it and forget it.
            self.store.set_enabled(sid, False)
            self.supervisor.wake()
            self._wait_until_stopped(sid)
            self.store.remove(sid)
            return {"ok": True, "title": entry.title, "trashed": False}
        transcript = self._transcript(sid)
        self._refuse_if_open_elsewhere(sid, "delete")
        entry = self.store.load().get(sid)
        if entry and entry.enabled:
            self.store.set_enabled(sid, False)
            self.supervisor.wake()
        self._wait_until_stopped(sid)
        self.store.remove(sid)
        item = self.trash.put(sid, transcript.path, transcript.title, transcript.project)
        return {"ok": True, "title": item.title, "trashed": True}

    def move(self, sid: str, cwd) -> dict:
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError("folder must be a non-empty path")
        new_cwd = os.path.normpath(os.path.expanduser(cwd.strip()))
        if not os.path.isabs(new_cwd):
            raise ValueError("folder must be an absolute path (or start with ~)")
        if not os.path.isdir(new_cwd):
            raise ValueError(f"{new_cwd} is not an existing folder")
        transcript = self._transcript(sid)
        if new_cwd == transcript.cwd:
            return {"ok": True, "cwd": new_cwd, "moved": False}
        self._refuse_if_open_elsewhere(sid, "move")
        entry = self.store.load().get(sid)
        kept = bool(entry and entry.enabled)
        if kept:
            self.store.set_enabled(sid, False)
            self.supervisor.wake()
        try:
            self._wait_until_stopped(sid)
            relocate(transcript.path, sid, transcript.cwd, new_cwd, self.index.projects_dir)
        except FileExistsError as e:
            raise Conflict(str(e)) from e
        finally:
            if kept:
                self.store.set_enabled(sid, True)
                self.supervisor.wake()
        return {"ok": True, "cwd": new_cwd, "moved": True}

    def restore(self, sid: str) -> dict:
        try:
            path = self.trash.restore(sid)
        except FileExistsError as e:
            raise Conflict(str(e)) from e
        return {"ok": True, "path": str(path)}

    def _transcript(self, sid: str):
        transcript = self.index.get(sid)
        if transcript is None:
            raise LookupError("no transcript with that session id")
        return transcript

    def _refuse_if_open_elsewhere(self, sid: str, verb: str) -> None:
        others = [p for p in self.registry() if p.session_id == sid and p.managed_for != sid]
        if others:
            raise Conflict(f"Open in another claude process (pid {others[0].pid}); close it before you {verb} it.")

    def _window(self, sid: str):
        return next((w for w in self.tmux.windows() if w.session_id == sid and not w.dead), None)

    def _wait_until_stopped(self, sid: str) -> None:
        deadline = time.monotonic() + _STOP_TIMEOUT_S
        while self._window(sid) or any(p.managed_for == sid for p in self.registry()):
            if time.monotonic() > deadline:
                raise Conflict(f"{short_id(sid)} didn't stop within {_STOP_TIMEOUT_S}s; try again.")
            self.sleep(0.5)
