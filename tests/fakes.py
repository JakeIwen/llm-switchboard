"""Stand-ins for tmux, the transcript index and the network, for supervisor and web tests."""

from dataclasses import replace
from pathlib import Path

from claude_always_remote.tmux import Window
from claude_always_remote.transcripts import Transcript

SID = "c96934f7-dd47-4470-a139-7d699146f4de"
SID2 = "0c598c4d-e1af-4b9a-a82e-1e348a4a1424"


def transcript(sid=SID, title="Telemetrics app changes", cwd="/home/pi/dev/obd-things"):
    return Transcript(session_id=sid, path=Path(f"/x/{sid}.jsonl"), cwd=cwd, title=title,
                      first_prompt="hi", mtime=1000.0, size=10)


class FakeIndex:
    def __init__(self, *transcripts):
        self.items = {t.session_id: t for t in transcripts}

    def all(self):
        return list(self.items.values())

    def get(self, sid):
        return self.items.get(sid)


class FakeTmux:
    def __init__(self):
        self.wins: dict[str, Window] = {}
        self.screens: dict[str, str] = {}
        self.started = []
        self.killed = []
        self._n = 0

    def windows(self):
        return list(self.wins.values())

    def start(self, session_id, label, command, env):
        self._n += 1
        wid = f"@{self._n}"
        self.wins[wid] = Window(wid, session_id, label, 100 + self._n, False, None)
        self.started.append((session_id, command, env))
        return wid

    def kill(self, window_id):
        self.wins.pop(window_id, None)
        self.killed.append(window_id)

    def capture(self, window_id, history=0):
        return self.screens.get(window_id, "")

    def window_for(self, sid):
        return next(w for w in self.wins.values() if w.session_id == sid)

    def exit(self, sid, status):
        w = self.window_for(sid)
        self.wins[w.window_id] = replace(w, dead=True, exit_status=status)


class FakeConnectivity:
    def __init__(self, up=True):
        self.up = up

    def ok(self):
        return self.up


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t
