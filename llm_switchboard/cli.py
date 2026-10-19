"""Command line: run the service, or enable/disable/inspect sessions from a shell."""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from .catalog import session_rows, sort_rows
from .connectivity import Connectivity
from .host_cli import add_parsers
from .hosts import Hosts
from .live_registry import live_processes
from .notify import Notifier, default_script
from .listen import Listeners
from .paths import CODEX_SOCKET, DEFAULT_HOSTS, DEFAULT_PORT
from .store import Store
from .supervisor import Supervisor
from .tmux import Tmux
from .transcripts import TranscriptIndex
from .trash import Trash
from .web import make_app, make_server


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.func(args)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="llm-switchboard",
        description="Manage Claude Code and Codex sessions, and keep chosen Claude Code sessions running in tmux with Remote Control on.",
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"web/API port (default {DEFAULT_PORT})")
    sub = p.add_subparsers(required=True, metavar="command")

    s = sub.add_parser("serve", help="run the supervisor and the picker web app (what the service runs)")
    s.add_argument("--host", action="append", metavar="ADDR",
                   help="address to listen on; repeat for more; `tailscale` means this machine's Tailscale "
                        f"address (default: {' and '.join(DEFAULT_HOSTS)}). Only localhost and Tailscale "
                        "peers are answered on any of them")
    s.add_argument("--interval", type=float, default=5.0, help="seconds between supervisor passes")
    s.set_defaults(func=_serve)

    s = sub.add_parser("list", help="list sessions (enabled first)")
    s.add_argument("-n", type=int, default=20, help="how many to show (default 20, 0 for all)")
    s.set_defaults(func=_list)

    s = sub.add_parser("status", help="show the enabled sessions and what the supervisor is doing")
    s.set_defaults(func=_status)

    for name, func, helptext in [
        ("enable", _enable, "keep a session running with Remote Control"),
        ("disable", _disable, "stop keeping a session running (closes its window)"),
        ("restart", _restart, "relaunch an enabled session now"),
        ("attach", _attach, "open an enabled session's tmux window in this terminal"),
    ]:
        s = sub.add_parser(name, help=helptext)
        s.add_argument("session", help="session id, or a unique prefix of one")
        if name == "enable":
            s.add_argument("--name", help="Remote Control display name (default: the session's title)")
        s.set_defaults(func=func)

    s = sub.add_parser("new", help="start a new session in a folder that already has sessions")
    s.add_argument("folder", help="working folder (one listed by `new --list`)", nargs="?")
    s.add_argument("name", help="session name", nargs="?")
    s.add_argument("--list", action="store_true", help="show the folders a new session can use")
    s.add_argument("--codex", metavar="FIRST_MESSAGE",
                   help="make it a Codex session that starts working on this message")
    s.set_defaults(func=_new)

    s = sub.add_parser("fork", help="copy a session's conversation so far into a new session")
    s.add_argument("session", help="session id, a unique prefix of one, or the short id `list` shows")
    s.add_argument("name", help="name for the copy (unique in its folder)")
    s.add_argument("--folder", help="working folder for the copy (default: the original's)")
    s.add_argument("--keep", action="store_true", help="keep the copy running with Remote Control")
    s.set_defaults(func=_fork)

    s = sub.add_parser("stop-all", help="kill the tmux server and every session in it")
    s.set_defaults(func=_stop_all)
    add_parsers(sub)
    return p


def _serve(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store, index, tmux = Store(), TranscriptIndex(), Tmux()
    supervisor = Supervisor(store, index, tmux, Connectivity(), interval_s=args.interval,
                            notifier=Notifier(default_script()))
    app = make_app(store, index, supervisor, tmux, Trash(), CODEX_SOCKET, hosts=Hosts())
    listeners = Listeners(args.host or list(DEFAULT_HOSTS), args.port, lambda host, port: make_server(app, host, port))
    stop = threading.Event()

    def shutdown(*_):
        stop.set()
        supervisor.wake()
        threading.Thread(target=listeners.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker = threading.Thread(target=supervisor.run_forever, args=(stop,), name="supervisor")
    worker.start()
    listeners.serve()
    worker.join(timeout=10)
    # The tmux server is left running on purpose: restarting this service
    # must not interrupt the Claude sessions. `stop-all` closes them.
    return 0


def _list(args) -> int:
    rows = _rows(args.port)
    shown = rows if args.n == 0 else rows[: args.n]
    for r in shown:
        mark = "●" if r["enabled"] else ("○" if r["open_elsewhere"] else " ")
        state = (r["status"] or {}).get("state", "")
        state = state or ("codex" if r.get("tool") == "codex" else "")
        print(f"{mark} {r['short_id']}  {_age(r['mtime']):>4}  {state:<9}  {r['project'][:28]:<28}  {r['title'][:60]}")
    if len(shown) < len(rows):
        print(f"… {len(rows) - len(shown)} more (-n 0 for all)")
    print("● kept running   ○ open in another claude process")
    return 0


def _status(args) -> int:
    rows = [r for r in _rows(args.port) if r["enabled"]]
    if not rows:
        print("No sessions are enabled.")
    for r in rows:
        st = r["status"] or {}
        mem = f"{st['rss_kb'] // 1024}MB" if st.get("rss_kb") else ""
        print(f"{r['short_id']}  {st.get('state', 'unknown'):<9} {mem:>6}  {r['project']}  {r['title'][:60]}")
        if st.get("detail"):
            print(f"          {st['detail']}")
    return 0


def _enable(args) -> int:
    sid = _resolve(args.session, transcripts_only=True)
    store = Store()
    store.set_enabled(sid, True)
    if args.name is not None:
        store.set_name(sid, args.name)
    print(f"Enabled {sid}; the service starts it within a few seconds.")
    return 0


def _disable(args) -> int:
    sid = _resolve(args.session)
    Store().set_enabled(sid, False)
    print(f"Disabled {sid}; its window closes within a few seconds.")
    return 0


def _restart(args) -> int:
    sid = _resolve(args.session)
    _post(args.port, f"/api/sessions/{sid}/restart")
    print(f"Restart requested for {sid}.")
    return 0


def _attach(args) -> int:
    sid = _resolve(args.session)
    tmux = Tmux()
    window = next((w for w in tmux.windows() if w.session_id == sid), None)
    if window is None:
        print(f"{sid} has no window; is it enabled and has the service started it?", file=sys.stderr)
        return 1
    argv = tmux.attach_argv(window.window_id)
    # Inside another tmux, attaching refuses unless $TMUX is cleared; nesting
    # is what's wanted here since this is a separate server.
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    os.execvpe(argv[0], argv, env)


def _new(args) -> int:
    if args.list or not (args.folder and args.name):
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/folders", timeout=10) as resp:
            for f in json.load(resp)["folders"]:
                blocked = f["codex_blocked"] if args.codex else f["blocked"]
                print(f"{'  ' if not blocked else '✗ '}{f['cwd']}  ({f['sessions']})  {blocked}")
        return 0 if args.list else 2
    folder = os.path.normpath(os.path.abspath(os.path.expanduser(args.folder)))
    if args.codex:
        result = _post(args.port, "/api/sessions/new",
                       {"tool": "codex", "cwd": folder, "name": args.name, "prompt": args.codex})
        print(f"Started Codex session {result['id']} ({result['title']}); it's working on your message. "
              f"Open it with: cd {folder} && codexp resume {result['id']}")
        return 0
    result = _post(args.port, "/api/sessions/new", {"cwd": folder, "name": args.name})
    print(f"Starting {result['id']} ({result['title']}); `llm-switchboard status` shows when it's ready.")
    return 0


def _fork(args) -> int:
    # Through the service's rows, so Codex sessions resolve too.
    ids = {r["id"] for r in _rows(args.port) if r["id"].startswith(args.session) or r["short_id"] == args.session}
    if len(ids) != 1:
        sys.exit(f"{'no session matches' if not ids else f'{len(ids)} sessions match'} {args.session!r}")
    sid = ids.pop()
    folder = os.path.normpath(os.path.abspath(os.path.expanduser(args.folder))) if args.folder else ""
    result = _post(args.port, f"/api/sessions/{sid}/fork", {"cwd": folder, "title": args.name, "keep": args.keep})
    print(f"Forked as {result['id']} ({result['title']}) in {result['cwd']}"
          + ("; `llm-switchboard status` shows when it's ready." if args.keep else "."))
    return 0


def _stop_all(args) -> int:
    Tmux().kill_server()
    print("Killed the tmux server. Enabled sessions come back on the next supervisor pass "
          "unless the service is stopped: systemctl --user stop llm-switchboard")
    return 0


def _resolve(prefix: str, transcripts_only: bool = False) -> str:
    ids = {t.session_id for t in TranscriptIndex().resolve(prefix)}
    if not transcripts_only:
        ids |= {sid for sid in Store().load() if sid.startswith(prefix)}
    if len(ids) != 1:
        problem = "no session matches" if not ids else f"{len(ids)} sessions match"
        sys.exit(f"{problem} {prefix!r}")
    return ids.pop()


def _rows(port: int) -> list[dict]:
    """Rows from the running service, or built locally (without supervisor state) if it's down."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=10) as resp:
            return json.load(resp)["sessions"]
    except (urllib.error.URLError, OSError):
        print("(service not reachable; showing sessions without supervisor state)", file=sys.stderr)
        return sort_rows(session_rows(TranscriptIndex().all(), Store().load(), live_processes(), {}))


def _post(port: int, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body or {}).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"{path}: {json.load(e).get('error', e.reason)}")
    except urllib.error.URLError as e:
        sys.exit(f"service not reachable on port {port}: {e}")


def _age(mtime: float | None) -> str:
    if not mtime:
        return "-"
    secs = time.time() - mtime
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return "now"
