"""Rename, delete and restore sessions without fighting a running claude process.

A running process keeps its own idea of the title and keeps appending to its
transcript, so:
- rename goes through `/rename` typed into the session when we run it, is
  appended as a custom-title record when nothing has it open, and is refused
  when another claude process has it open;
- delete stops a kept-running session first, is refused while another claude
  process has it open, and moves the files to the trash so it can be undone;
- move (to another working folder) stops a kept-running session, moves its
  files, and lets the supervisor relaunch it in the new folder;
- fork copies the conversation so far into a new session, leaving the
  original (running or not) as it is.

Names stay unique per folder (see names.py).
"""

import time
import uuid

from .conflict import Conflict
from .folders import existing_folder, new_session_folders
from .live_registry import live_processes
from .names import Names, claude_names, clean_title
from .relocate import fork, relocate
from .screen import blocking_prompt
from .session_id import short_id
from .store import Store
from .supervisor import Supervisor
from .tmux import Tmux
from .transcripts import TranscriptIndex, custom_title_record
from .trash import Trash

_STOP_TIMEOUT_S = 20


class SessionActions:
    def __init__(self, store: Store, index: TranscriptIndex, supervisor: Supervisor, tmux: Tmux, trash: Trash,
                 registry=live_processes, sleep=time.sleep, folders=new_session_folders, names: Names | None = None):
        self.store = store
        self.index = index
        self.supervisor = supervisor
        self.tmux = tmux
        self.trash = trash
        self.registry = registry
        self.sleep = sleep
        self.folders = folders
        self.names = names or Names(claude_names(index, store))

    def rename(self, sid: str, title) -> dict:
        title = clean_title(title)
        transcript = self._transcript(sid)
        self._refuse_if_open_elsewhere(sid, "rename")
        self.names.refuse_duplicate(transcript.cwd, title, sid)
        window = self._window(sid)
        if window is None:
            with open(transcript.path, "ab") as f:
                f.write(custom_title_record(sid, title))
            return {"ok": True, "title": title, "via": "transcript"}
        ours = next((p for p in self.registry() if p.managed_for == sid), None)
        if ours is None or ours.status != "idle" or blocking_prompt(self.tmux.capture(window.window_id)):
            raise Conflict("The session is starting, working or waiting on a prompt; rename it when it's idle.")
        self.tmux.send_line(window.window_id, f"/rename {title}")
        return {"ok": True, "title": title, "via": "running session"}

    def create(self, cwd, title) -> dict:
        """Start a brand-new session in one of the offered folders, kept running."""
        title = clean_title(title)
        folder = next((f for f in self.folders(self.index.all()) if f.cwd == cwd), None)
        if folder is None:
            raise ValueError("pick one of the listed folders")
        if folder.blocked:
            raise Conflict(folder.blocked)
        self.names.refuse_duplicate(cwd, title)
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
        new_cwd = existing_folder(cwd)
        transcript = self._transcript(sid)
        if new_cwd == transcript.cwd:
            return {"ok": True, "cwd": new_cwd, "moved": False}
        self._refuse_if_open_elsewhere(sid, "move")
        self.names.refuse_duplicate(new_cwd, transcript.title, sid, "rename one of them first")
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

    def fork(self, sid: str, cwd, title, keep: bool = False) -> dict:
        """Copy a conversation into a new session in cwd (default: the same folder)."""
        title = clean_title(title)
        transcript = self._transcript(sid)
        new_cwd = existing_folder(cwd) if cwd else transcript.cwd
        self.names.refuse_duplicate(new_cwd, title)
        new_sid = str(uuid.uuid4())
        fork(transcript.path, sid, new_sid, transcript.cwd, new_cwd, title, self.index.projects_dir)
        if keep:
            self.store.set_enabled(new_sid, True)
            self.supervisor.announce_when_ready(new_sid)
        return {"ok": True, "id": new_sid, "title": title, "cwd": new_cwd}

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

