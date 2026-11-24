"""Mac sessions asking to join (docs/voice-desk.md, "Enrollment for Mac
sessions", Candidates): a Mac `register` the hub refused only because the
session isn't enrolled adds or refreshes one entry `{host, session_id,
label, folder, seen}`, which the desk page offers with **Enroll now**.

Each entry is small whatever the frame carried (the label cut to
MAX_CANDIDATE_LABEL characters, the folder the last two components of the
cwd cut to MAX_CANDIDATE_FOLDER), there are at most MAX_CANDIDATES (the
longest unseen evicted), and each is forgotten CANDIDATE_TTL_SECONDS after
its last attempt. A session just unenrolled for a host keeps retrying from
its running channel, so it stays off the list for as long, in a second
table of the same size (oldest evicted). In memory only; the wall clock is
injected. Its own lock is never held while another lock is taken.
"""

from collections import OrderedDict
import threading
from typing import Callable

from .labels import encodable
from .model import CANDIDATE_TTL_SECONDS, MAX_CANDIDATE_FOLDER, MAX_CANDIDATE_LABEL, MAX_CANDIDATES


def candidate_folder(cwd: str) -> str:
    """The last two components of a Mac cwd, cut to MAX_CANDIDATE_FOLDER."""
    return "/".join([part for part in cwd.split("/") if part][-2:])[:MAX_CANDIDATE_FOLDER]


class Candidates:
    def __init__(self, now: Callable[[], float]):
        self._now = now
        self._lock = threading.Lock()
        # (host, session id) -> entry, least recently seen first.
        self._entries: OrderedDict[tuple[str, str], dict] = OrderedDict()
        # (host, session id) -> when it was unenrolled, oldest first.
        self._unenrolled: OrderedDict[tuple[str, str], float] = OrderedDict()

    def seen(self, host: str, session_id: str, label: str, cwd: str) -> None:
        now = self._now()
        key = (host, session_id)
        with self._lock:
            self._prune(now)
            if key in self._unenrolled:
                return
            self._entries.pop(key, None)
            self._entries[key] = {"host": host, "session_id": session_id,
                                  "label": encodable(label[:MAX_CANDIDATE_LABEL]),
                                  "folder": encodable(candidate_folder(cwd)), "seen": now}
            while len(self._entries) > MAX_CANDIDATES:
                self._entries.popitem(last=False)

    def enrolled(self, host: str, session_id: str) -> None:
        with self._lock:
            self._entries.pop((host, session_id), None)

    def unenrolled(self, host: str, session_id: str) -> None:
        now = self._now()
        key = (host, session_id)
        with self._lock:
            self._prune(now)
            self._entries.pop(key, None)
            self._unenrolled.pop(key, None)
            self._unenrolled[key] = now
            while len(self._unenrolled) > MAX_CANDIDATES:
                self._unenrolled.popitem(last=False)

    def view(self) -> list[dict]:
        """The live entries, ordered by host and session id."""
        with self._lock:
            self._prune(self._now())
            return [dict(self._entries[key]) for key in sorted(self._entries)]

    def _prune(self, now: float) -> None:
        for key in [k for k, entry in self._entries.items() if now - entry["seen"] >= CANDIDATE_TTL_SECONDS]:
            del self._entries[key]
        for key in [k for k, since in self._unenrolled.items() if now - since >= CANDIDATE_TTL_SECONDS]:
            del self._unenrolled[key]
