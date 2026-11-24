"""The desk relay process on the Mac (docs/voice-desk.md, "Mac link":
"Processes, sockets and files", "Bounds", "Privacy").

`main` is what `bin/desk-relay` runs under launchd
(`dev.llm-switchboard.desk-relay`), `run_process(config, launched=False)`
what `llm-switchboard desk relay` runs in a terminal. Both:

1. take `relay.lock` for the life of the process (run_lock), so only one
   relay runs; a second exits 3 before it binds or opens its log;
2. log through a `RotatingFileHandler` on `DeskConfig.relay_log` (1 MiB, one
   backup) and send uncaught exceptions of every thread there
   (`sys.excepthook`, `threading.excepthook`);
3. under launchd only, point stderr at /dev/null: bin/desk-relay pointed it
   at the launch log so a failed start leaves its traceback there, and
   from here on nothing more may reach that log;
4. `serve`: bind the channel socket (relay_channels), then run links one
   after another (relay_link's `RelayConnector`) until SIGTERM or SIGINT,
   when the current link is retired and the channel socket closed.

Logs carry link states, codes, paths and the build, never text from ssh, a
channel or the hub. Standard library only.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
import threading

from . import mac_machine
from .checkout_build import checkout_build
from .config import DeskConfig
from .private_socket import SocketInUse
from .relay_channels import RelayChannels
from .relay_link import RelayConnector
from .run_lock import START_WAIT_SECONDS, RunLockHeld, RunLockUnusable, hold_for_process

EXIT_FAILED = 1
EXIT_SETUP = 2
EXIT_RUN_LOCK_HELD = 3

RELAY_LOG_BYTES = 1024 * 1024
RELAY_LOG_BACKUPS = 1
# How long a stop waits for the connector to return: a link's retirement
# ends its ssh child within child_process's 2 s grace plus SIGKILL's landing.
STOP_JOIN_SECONDS = 10.0
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

log = logging.getLogger("llm_switchboard.desk.relay")


def main() -> int:
    """bin/desk-relay, under launchd: stderr is the launch log until logging starts."""
    return run_process(DeskConfig.from_env(), launched=True)


def run_process(config: DeskConfig, *, launched: bool) -> int:
    """The whole relay process; its exit status. Takes process-wide state
    (the run lock, the root logger, the exception hooks, signal handlers,
    fd 2 under launchd), so it runs once per process, on the main thread."""
    try:
        hold_for_process(config.relay_lock, wait_seconds=START_WAIT_SECONDS)
    except RunLockHeld:
        print("desk relay: another desk relay holds relay.lock", file=sys.stderr)
        return EXIT_RUN_LOCK_HELD
    except RunLockUnusable as exc:
        print(f"desk relay: {exc}", file=sys.stderr)
        return EXIT_SETUP
    start_logging(config, console=not launched)
    if launched:
        close_launch_log()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    build = checkout_build(config.repo)
    log.info("desk relay starting (build %s)", build or "unknown")
    return serve(config, stop, relay_build=build)


def start_logging(config: DeskConfig, *, console: bool) -> None:
    """The rotating log on the root logger, and every uncaught exception into it."""
    config.relay_log.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.relay_log, maxBytes=RELAY_LOG_BYTES, backupCount=RELAY_LOG_BACKUPS,
                                  encoding="utf-8")
    handlers: list[logging.Handler] = [handler]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    root = logging.getLogger()
    for each in handlers:
        each.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(each)
    root.setLevel(logging.INFO)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_in_thread


def _log_uncaught(exc_type, exc_value, exc_traceback) -> None:
    log.critical("desk relay: uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


def _log_uncaught_in_thread(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "?"
    log.error("desk relay: uncaught exception in thread %s", name,
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def close_launch_log() -> None:
    """fd 2 becomes /dev/null, so the launch log never grows once logging runs."""
    null = os.open(os.devnull, os.O_WRONLY | os.O_CLOEXEC)
    try:
        os.dup2(null, 2)
    finally:
        os.close(null)


def serve(config: DeskConfig, stop: threading.Event, *, relay_build: str = "", machine: str | None = None,
          channels_factory=None, connector_factory=None) -> int:
    """Bind the channel socket and run links until `stop` is set (or the
    connector's thread dies); then retire the current link and close the
    socket. The factories are tests' seams; unset, they are this module's
    `RelayChannels` and `RelayConnector`, looked up at the call."""
    try:
        machine = mac_machine.machine_id() if machine is None else machine
    except OSError:
        log.error("desk relay: this Mac's hardware UUID can't be read; not starting")
        return EXIT_SETUP
    channels = (channels_factory or RelayChannels)(config)
    try:
        channels.start()
    except (SocketInUse, ValueError, OSError) as exc:
        log.error("desk relay: can't listen on the channel socket: %s", exc)
        channels.close()
        return EXIT_SETUP
    connector = (connector_factory or RelayConnector)(config, channels, machine=machine, relay_build=relay_build)
    thread = threading.Thread(target=connector.run, name="relay-connector", daemon=True)
    thread.start()
    try:
        while not stop.wait(1.0) and thread.is_alive():
            pass
    finally:
        log.info("desk relay stopping")
        connector.stop()
        channels.close()
        thread.join(STOP_JOIN_SECONDS)
        if thread.is_alive():
            log.error("desk relay: the link didn't end within %.0f s of the stop", STOP_JOIN_SECONDS)
    return 0 if stop.is_set() else EXIT_FAILED
