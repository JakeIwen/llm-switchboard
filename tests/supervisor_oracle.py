"""Deterministic, offline behavior capture for supervisor reorganizations.

Run with the checkout under test on PYTHONPATH, for example:
PYTHONPATH=/path/to/checkout python3 tests/supervisor_oracle.py
"""

from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from llm_switchboard.catalog import session_rows
from llm_switchboard.live_registry import LiveProcess
from llm_switchboard.store import Store
from llm_switchboard.supervisor import RC_GRACE_S, Supervisor
from llm_switchboard.tmux import TmuxError, Window
from llm_switchboard.transcripts import Transcript

SID = "c96934f7-dd47-4470-a139-7d699146f4de"
MISSING = "11111111-2222-3333-4444-555555555555"


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


class Index:
    def __init__(self, present=True):
        item = Transcript(SID, Path("/fixture/session.jsonl"), "/fixture/project",
                          "Fixture title", "hi", 1.0, 2)
        self.items = {SID: item} if present else {}

    def get(self, sid):
        return self.items.get(sid)


class Network:
    def __init__(self, up=True):
        self.up = up

    def ok(self):
        return self.up


class EventsStore:
    def __init__(self, path, events):
        self.real = Store(path)
        self.events = events

    def load(self):
        self.events.append("store")
        return self.real.load()


class EventsTmux:
    def __init__(self, events):
        self.events = events
        self.wins = {}
        self.screens = {}
        self.fail_start = False
        self.n = 0

    def windows(self):
        self.events.append("windows")
        return list(self.wins.values())

    def start(self, sid, label, command, env):
        self.events.append({"op": "start", "sid": sid, "label": label,
                            "command": command, "env": env})
        if self.fail_start:
            raise TmuxError("fixture start failure")
        self.n += 1
        wid = f"@{self.n}"
        self.wins[wid] = Window(wid, sid, label, 100 + self.n, False, None)
        return wid

    def kill(self, wid):
        self.events.append("kill")
        self.wins.pop(wid, None)

    def capture(self, wid, history=0):
        self.events.append(f"capture:{history}")
        return self.screens.get(wid, "")

    def window(self):
        return next(iter(self.wins.values()))

    def exit(self, code):
        window = self.window()
        self.wins[window.window_id] = replace(window, dead=True, exit_status=code)


class Notifications:
    def __init__(self, events):
        self.events = events

    def send(self, title, message, priority="default", tags=""):
        self.events.append(f"notify:{priority}:{title}:{message}")


def status_json(supervisor):
    result = {}
    for sid, status in supervisor.snapshot().items():
        raw = asdict(status)
        raw["state"] = status.state.value
        result[sid] = raw
    return result


def record(supervisor, events, expected_state, expected_events, sid=SID):
    statuses = status_json(supervisor)
    actual_state = statuses.get(sid, {}).get("state")
    assert actual_state == expected_state, (actual_state, expected_state)
    operations = [event.get("op") if isinstance(event, dict) else event.split(":", 1)[0]
                  for event in events]
    assert operations == expected_events, (operations, expected_events)
    # Copy now: several cases continue reconciling with the same collaborators.
    rows = session_rows(list(supervisor.index.items.values()), supervisor.store.real.load(),
                        [], supervisor.snapshot())
    return {"events": json.loads(json.dumps(events)), "status": statuses, "rows": rows,
            "store": supervisor.store.real.path.read_text()}


def fixture(*, transcript=True, network=True, live=None):
    temp = tempfile.TemporaryDirectory()
    events = []
    store = EventsStore(Path(temp.name) / "sessions.json", events)
    tmux = EventsTmux(events)
    clock = Clock()
    live_items = live if live is not None else []
    supervisor = Supervisor(store, Index(transcript), tmux, Network(network),
                            registry=lambda: live_items, clock=clock,
                            notifier=Notifications(events))
    return temp, events, store, tmux, clock, supervisor


def capture_running_and_new():
    output = {}
    temp, events, store, tmux, clock, sup = fixture()
    store.real.set_enabled(SID, True)
    sup.announce_when_ready(SID)
    sup.reconcile()
    tmux.screens[tmux.window().window_id] = "/rc active"
    sup.reconcile()
    output["launch_running"] = record(
        sup, events, "running", ["store", "windows", "start", "store", "windows",
                                 "capture", "notify"],
    )
    temp.cleanup()

    temp, events, store, tmux, clock, sup = fixture()
    store.real.create(SID, "/fixture/new", "New title")
    sup.reconcile()
    output["new"] = record(sup, events, "starting", ["store", "windows", "start"])
    temp.cleanup()
    return output


def capture_duplicate_process():
    output = {}
    duplicate = [LiveProcess(42, SID, "/fixture/project", "idle", None, 1)]
    temp, events, store, tmux, clock, sup = fixture(live=duplicate)
    store.real.set_enabled(SID, True)
    sup.reconcile()
    output["elsewhere"] = record(sup, events, "elsewhere", ["store", "windows"])
    duplicate[:] = []
    sup.reconcile()
    duplicate.append(LiveProcess(43, SID, "/fixture/project", "idle", None, 1))
    tmux.screens[tmux.window().window_id] = "Enter to confirm"
    sup.reconcile()
    output["blocked_duplicate"] = record(
        sup, events, "attention",
        ["store", "windows", "store", "windows", "start", "store", "windows", "capture"],
    )
    assert output["blocked_duplicate"]["status"][SID]["detail"] == (
        "A prompt is waiting for input; attach to answer it. "
        "Also open in another claude process (pid 43)."
    )
    temp.cleanup()
    return output


def capture_backoff():
    output = {}
    temp, events, store, tmux, clock, sup = fixture()
    store.real.set_enabled(SID, True)
    sup.reconcile()
    tmux.exit(7)
    sup.reconcile()
    clock.t = sup.snapshot()[SID].next_attempt
    sup.reconcile()
    tmux.exit(8)
    sup.reconcile()
    output["two_crashes_backoff"] = record(
        sup, events, "backoff",
        ["store", "windows", "start", "store", "windows", "capture", "kill",
         "store", "windows", "start", "store", "windows", "capture", "kill"],
    )
    assert output["two_crashes_backoff"]["status"][SID]["failures"] == 2
    temp.cleanup()
    return output


def capture_restart_and_disable():
    output = {}
    temp, events, store, tmux, clock, sup = fixture()
    store.real.set_enabled(SID, True)
    sup.reconcile()
    adopted = Supervisor(store, Index(), tmux, Network(), registry=lambda: [],
                         clock=clock, notifier=Notifications(events))
    adopted.reconcile()
    adopted.request_restart(SID)
    adopted.reconcile()
    store.real.set_enabled(SID, False)
    adopted.reconcile()
    output["adopt_restart_disable"] = record(
        adopted, events, None,
        ["store", "windows", "start", "store", "windows", "capture", "store",
         "windows", "kill", "start", "store", "windows", "kill"],
    )
    temp.cleanup()
    return output


def capture_unavailable():
    output = {}
    temp, events, store, tmux, clock, sup = fixture()
    store.real.set_enabled(MISSING, True)
    sup.reconcile()
    output["missing"] = record(sup, events, "missing", ["store", "windows"], MISSING)
    temp.cleanup()

    temp, events, store, tmux, clock, sup = fixture(network=False)
    store.real.set_enabled(SID, True)
    sup.reconcile()
    output["offline"] = record(sup, events, "offline", ["store", "windows"])
    temp.cleanup()

    temp, events, store, tmux, clock, sup = fixture()
    store.real.set_enabled(SID, True)
    tmux.fail_start = True
    sup.reconcile()
    output["tmux_error"] = record(sup, events, "error", ["store", "windows", "start"])
    assert output["tmux_error"]["status"][SID]["detail"] == "fixture start failure"
    temp.cleanup()
    return output


def capture():
    with patch("llm_switchboard.store.time.time", return_value=1_700_000_000.0):
        return {**capture_running_and_new(), **capture_duplicate_process(), **capture_backoff(),
                **capture_restart_and_disable(), **capture_unavailable()}


if __name__ == "__main__":
    print(json.dumps(capture(), sort_keys=True, separators=(",", ":")))
