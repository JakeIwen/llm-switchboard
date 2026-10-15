"""Rename, delete, restore, fork and create Codex sessions, through the Codex daemon.

The daemon owns every thread's state, so these are API calls, never file
edits:
- delete archives the thread (Codex's soft delete) and notes it in the
  CodexTrash; restore unarchives it; after 30 days, or on "Empty now", it is
  deleted for good. Codex refuses to delete a thread that a fork's history
  still points at; such a thread just stays archived in Codex.
- a new session needs a first message: Codex saves a thread only once it has
  a turn, so create starts the thread and its first turn, which the daemon
  then works on in the background.
- there is no move: Codex has no call to change a thread's folder (fork it
  into the other folder instead), and no keep-running, since the daemon
  already holds every thread.
"""

import time

from .codex_client import CodexClient, CodexError
from .codex_threads import CodexCatalog, CodexState, CodexThread
from .codex_trash import CodexTrash, CodexTrashed
from .conflict import Conflict
from .folders import existing_folder
from .names import Names, clean_title
from .trash import RETENTION_S
from .transcripts import display_path

# What the codexp shell function passes (--ask-for-approval never). A thread
# started here works on its first turn with nobody there to approve anything.
APPROVAL_POLICY = "never"
MAX_PROMPT_LEN = 20_000


class CodexActions:
    def __init__(self, client: CodexClient, catalog: CodexCatalog, trash: CodexTrash, names: Names, clock=time.time):
        self.client = client
        self.catalog = catalog
        self.trash = trash
        self.names = names
        self.clock = clock

    def owns(self, sid: str) -> bool:
        return self.catalog.get(sid) is not None or self.trash.get(sid) is not None

    def act(self, sid: str, action: str, body: dict) -> dict:
        if action == "rename":
            return self.rename(sid, body.get("title"))
        if action == "delete":
            return self.delete(sid)
        if action == "restore":
            return self.restore(sid)
        if action == "fork":
            return self.fork(sid, body.get("cwd"), body.get("title"))
        raise ValueError(f"Codex sessions can't {action}" + ("; fork it into the other folder instead" if action == "move" else ""))

    def rename(self, sid: str, title) -> dict:
        title = clean_title(title)
        thread = self._thread(sid)
        self.names.refuse_duplicate(thread.cwd, title, sid)
        self._call("thread/name/set", {"threadId": sid, "name": title})
        return {"ok": True, "title": title, "via": "codex"}

    def delete(self, sid: str) -> dict:
        thread = self._thread(sid)
        if thread.state == CodexState.ACTIVE:
            raise Conflict("It's working on a turn; delete it once it's done.")
        self._call("thread/archive", {"threadId": sid})
        self.trash.put(CodexTrashed(sid, thread.title, thread.project, self.clock()))
        return {"ok": True, "title": thread.title, "trashed": True}

    def restore(self, sid: str) -> dict:
        item = self.trash.get(sid)
        if item is None:
            raise LookupError("not in the trash")
        self._call("thread/unarchive", {"threadId": sid})
        self.trash.remove(sid)
        return {"ok": True}

    def fork(self, sid: str, cwd, title) -> dict:
        """Copy a thread's conversation into a new thread in cwd (default: the same folder)."""
        title = clean_title(title)
        thread = self._thread(sid)
        new_cwd = existing_folder(cwd) if cwd else thread.cwd
        self.names.refuse_duplicate(new_cwd, title)
        try:
            with self.client.connect() as conn:
                new = _new_id(conn.call("thread/fork", {"threadId": sid, "cwd": new_cwd}))
                conn.call("thread/name/set", {"threadId": new, "name": title})
                conn.call("thread/unsubscribe", {"threadId": new})
        except CodexError as e:
            raise Conflict(f"Codex refused: {e}") from e
        finally:
            self.catalog.invalidate()
        return {"ok": True, "id": new, "title": title, "cwd": new_cwd}

    def create(self, cwd, title, prompt) -> dict:
        title = clean_title(title)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("a Codex session needs a first message (Codex saves a session only once it has one)")
        if len(prompt) > MAX_PROMPT_LEN:
            raise ValueError(f"the first message must be at most {MAX_PROMPT_LEN} characters")
        cwd = existing_folder(cwd)
        self.names.refuse_duplicate(cwd, title)
        try:
            with self.client.connect() as conn:
                new = _new_id(conn.call("thread/start", {"cwd": cwd, "approvalPolicy": APPROVAL_POLICY}))
                conn.call("thread/name/set", {"threadId": new, "name": title})
                conn.call("turn/start", {"threadId": new, "input": [{"type": "text", "text": prompt.strip()}]})
        except CodexError as e:
            raise Conflict(f"Codex refused: {e}") from e
        finally:
            self.catalog.invalidate()
        return {"ok": True, "id": new, "title": title, "cwd": cwd, "project": display_path(cwd)}

    def trash_items(self) -> list[CodexTrashed]:
        """Sessions deleted here, after deleting for good the ones past 30 days."""
        items = self.trash.items()
        expired = [i for i in items if self.clock() - i.deleted_at > RETENTION_S]
        for item in expired:
            self._purge(item)
        return [i for i in items if i not in expired]

    def empty_trash(self) -> int:
        items = self.trash.items()
        for item in items:
            self._purge(item)
        return len(items)

    def _purge(self, item: CodexTrashed) -> None:
        try:
            self.client.call("thread/delete", {"threadId": item.session_id})
        except CodexError:
            pass  # e.g. a fork's history still points at it; it stays archived in Codex
        self.trash.remove(item.session_id)

    def _thread(self, sid: str) -> CodexThread:
        thread = self.catalog.get(sid)
        if thread is None:
            raise LookupError("no Codex session with that id")
        return thread

    def _call(self, method: str, params: dict):
        try:
            return self.client.call(method, params)
        except CodexError as e:
            raise Conflict(f"Codex refused: {e}") from e
        finally:
            self.catalog.invalidate()


def _new_id(result) -> str:
    try:
        return str(result["thread"]["id"])
    except (KeyError, TypeError) as e:
        raise CodexError(f"unexpected reply: {str(result)[:200]}") from e
